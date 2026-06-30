"""Data coordinator for Solar Forecast.

Handles:
  - Pulling Open-Meteo daily + hourly forecasts.
  - Predicting kWh from radiation using either:
      * the learned linear model (kWh = a + b·GHI), or
      * a physics-derived fallback based on panel kWp + tilt + azimuth
        for first-run installs with no production history.
  - Persisting prediction/actual history in HA storage.
  - Self-recalibrating from accumulated history (refit on a schedule).
  - One-shot bootstrap that scans HA's recorder + Open-Meteo archive
    on first run when a production sensor is configured.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN, STORAGE_KEY, STORAGE_VERSION, OPENMETEO_FORECAST, OPENMETEO_ARCHIVE, MJ_TO_WH,
    CONF_LATITUDE, CONF_LONGITUDE,
    CONF_PRODUCTION_ENTITY, CONF_DAILY_ENERGY_ENTITY, CONF_SENSOR_KIND,
    CONF_PANEL_KWP, CONF_TILT, CONF_AZIMUTH,
    CONF_REFIT_DAYS, CONF_BOOTSTRAP_DAYS,
    CONF_PRICE_ENTITY, CONF_THROTTLE_SWITCH,
    DEFAULT_PANEL_KWP, DEFAULT_TILT, DEFAULT_AZIMUTH,
    DEFAULT_REFIT_DAYS, DEFAULT_BOOTSTRAP_DAYS,
    MIN_TRAINING_DAYS, RESID_THRESHOLD_RMSE_MULT, PV_DERATING,
)

_LOGGER = logging.getLogger(__name__)


def _tilt_azimuth_factor(tilt_deg: float, azimuth_deg: float, latitude_deg: float) -> float:
    """Rough efficiency factor relative to GHI-on-horizontal.

    Compares the panel's effective irradiance over a typical day to flat-plate GHI,
    using a simplified cosine-projection toward solar noon at the equinox sun-altitude.
    Returns ~1.0 for an optimally-tilted south-facing panel at mid-latitudes,
    lower for off-axis orientations or extreme tilts.

    This is intentionally a rough heuristic — once we have real production data
    the regression model takes over and replaces this entirely.
    """
    # Optimum tilt rule of thumb: ~ latitude. Penalise deviation linearly.
    tilt_opt = abs(latitude_deg)
    tilt_pen = max(0.0, 1.0 - 0.005 * abs(tilt_deg - tilt_opt))  # 1% per 2° off
    # Azimuth: 180° (south) is best in northern hemisphere; cos falloff away from south
    az_offset = abs(((azimuth_deg - 180.0 + 180.0) % 360.0) - 180.0)
    az_pen = max(0.3, math.cos(math.radians(az_offset)))  # never less than 0.3 (diffuse)
    return tilt_pen * az_pen


class SolarForecastCoordinator(DataUpdateCoordinator):
    """Polls Open-Meteo and applies the calibrated (or physics-default) model."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=timedelta(hours=3),
        )
        self.entry = entry
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._history: dict[str, dict] = {}      # date_iso -> {actual, predicted, ghi}
        # Model is None until either bootstrap or refit fills it. Until then,
        # predictions come from the physics fallback (panel kWp + tilt + azimuth).
        self._model: dict | None = None
        self._last_refit: str | None = None
        self._bootstrap_done: bool = False
        self._bootstrap_summary: dict | None = None

    @property
    def cfg(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    # ---------- storage ----------
    async def async_load_storage(self):
        data = await self._store.async_load() or {}
        self._history = data.get("history", {})
        self._model = data.get("model")  # may be None for fresh installs
        self._last_refit = data.get("last_refit")
        self._bootstrap_done = bool(data.get("bootstrap_done", False))
        self._bootstrap_summary = data.get("bootstrap_summary")
        # One-time cleanup: drop hourly_actual_kwh arrays where hour 0 holds a phantom
        # pre-reset spike (pre-1.2.1 bug). Triggers re-backfill on next update.
        cleaned = 0
        for d_iso, rec in self._history.items():
            ha = rec.get("hourly_actual_kwh")
            if not isinstance(ha, list) or len(ha) < 24:
                continue
            v0 = ha[0] if ha[0] is not None else 0
            rest_max = max((v for v in ha[1:] if v is not None), default=0)
            if v0 >= 8 and v0 >= 2 * rest_max:
                rec["hourly_actual_kwh"] = None
                cleaned += 1
        if cleaned:
            _LOGGER.info("Cleared %d corrupted hourly_actual_kwh arrays (phantom spike at hour 0)", cleaned)
            await self._save_storage()
        _LOGGER.info("Loaded %d historical days, model=%s", len(self._history), self._model)

    async def async_backfill_hourly_actuals(self, days_back: int = 30) -> int:
        """Backfill hourly_actual_kwh for recent days that have a daily total
        but no per-hour curve. Returns the number of days filled in."""
        today = dt_util.now().date()
        candidates = []
        for d_iso in sorted(self._history.keys(), reverse=True):
            try:
                d = date.fromisoformat(d_iso)
            except ValueError:
                continue
            if d >= today:
                continue
            if (today - d).days > days_back:
                break
            rec = self._history[d_iso]
            if rec.get("actual") is None:
                continue
            if rec.get("hourly_actual_kwh"):
                continue
            candidates.append(d)
        filled = 0
        for d in candidates:
            try:
                hourly = await self._read_hourly_actual_for(d)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Hourly backfill failed for %s: %s", d, err)
                continue
            if hourly and any(v is not None for v in hourly):
                self._history[d.isoformat()]["hourly_actual_kwh"] = hourly
                filled += 1
        if filled:
            await self._save_storage()
            _LOGGER.info("Backfilled hourly actuals for %d recent days", filled)
        return filled

    async def _save_storage(self):
        await self._store.async_save({
            "history": self._history,
            "model": self._model,
            "last_refit": self._last_refit,
            "bootstrap_done": self._bootstrap_done,
            "bootstrap_summary": self._bootstrap_summary,
        })

    # ---------- Open-Meteo fetch ----------
    async def _fetch_openmeteo(self) -> dict:
        cfg = self.cfg
        params = {
            "latitude": cfg[CONF_LATITUDE],
            "longitude": cfg[CONF_LONGITUDE],
            "daily": ",".join([
                "shortwave_radiation_sum", "sunshine_duration",
                "cloud_cover_mean", "temperature_2m_max", "temperature_2m_min",
                "precipitation_sum", "sunrise", "sunset", "uv_index_max", "weather_code",
            ]),
            "hourly": ",".join([
                "shortwave_radiation", "cloud_cover", "temperature_2m",
                "precipitation", "wind_speed_10m", "relative_humidity_2m",
            ]),
            "forecast_days": 7,
            "timezone": "auto",
        }
        session = async_get_clientsession(self.hass)
        try:
            async with asyncio.timeout(30):
                async with session.get(OPENMETEO_FORECAST, params=params) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Open-Meteo fetch failed: {err}") from err

    # ---------- model application ----------
    def _physics_coeffs(self) -> tuple[float, float]:
        """Return (intercept, slope) for the physics fallback model.

        kWh_day ≈ kWp × GHI(Wh/m²) / 1000 × derating × tilt_az_factor
        So slope = kWp/1000 × derating × tilt_az_factor and intercept = 0.
        """
        cfg = self.cfg
        kwp = float(cfg.get(CONF_PANEL_KWP, DEFAULT_PANEL_KWP))
        tilt = float(cfg.get(CONF_TILT, DEFAULT_TILT))
        az = float(cfg.get(CONF_AZIMUTH, DEFAULT_AZIMUTH))
        lat = float(cfg.get(CONF_LATITUDE, 0.0))
        f = _tilt_azimuth_factor(tilt, az, lat)
        slope = (kwp / 1000.0) * PV_DERATING * f
        return 0.0, slope

    def _active_model(self) -> dict:
        """Return the current effective model dict (learned or physics)."""
        if self._model and "slope" in self._model:
            return self._model
        a, b = self._physics_coeffs()
        return {
            "intercept": a, "slope": b, "source": "physics",
            "panel_kwp": float(self.cfg.get(CONF_PANEL_KWP, DEFAULT_PANEL_KWP)),
            "tilt": float(self.cfg.get(CONF_TILT, DEFAULT_TILT)),
            "azimuth": float(self.cfg.get(CONF_AZIMUTH, DEFAULT_AZIMUTH)),
        }

    def _predict_kwh(self, ghi_wh_m2: float) -> float:
        m = self._active_model()
        return max(0.0, m["intercept"] + m["slope"] * ghi_wh_m2)

    # ---------- main update ----------
    async def _async_update_data(self) -> dict:
        raw = await self._fetch_openmeteo()
        daily = raw["daily"]
        hourly = raw["hourly"]

        # daily predictions
        dates: list[str] = daily["time"]
        ghi_mj: list[float] = daily["shortwave_radiation_sum"]
        pred_kwh = [round(self._predict_kwh((g or 0) * MJ_TO_WH), 1) for g in ghi_mj]

        # store today's forecast + weather inputs in history
        today_iso = dt_util.now().date().isoformat()
        if today_iso in dates:
            idx = dates.index(today_iso)
            rec = self._history.get(today_iso, {})
            rec["predicted"] = pred_kwh[idx]
            rec["ghi_wh_m2"] = round((ghi_mj[idx] or 0) * MJ_TO_WH, 1)
            # capture daily weather inputs that drive the prediction
            try:
                sd = daily.get("sunshine_duration", [None] * len(dates))[idx]
                rec["sunshine_min"] = round((sd or 0) / 60) if sd is not None else None
                rec["cloud_pct"] = daily.get("cloud_cover_mean", [None] * len(dates))[idx]
                rec["temp_min"] = daily.get("temperature_2m_min", [None] * len(dates))[idx]
                rec["temp_max"] = daily.get("temperature_2m_max", [None] * len(dates))[idx]
                rec["precip_mm"] = daily.get("precipitation_sum", [None] * len(dates))[idx]
                rec["uv_max"] = daily.get("uv_index_max", [None] * len(dates))[idx]
                rec["weather_code"] = daily.get("weather_code", [None] * len(dates))[idx]
            except (IndexError, TypeError):
                pass
            self._history[today_iso] = rec
            # hourly arrays stored below (after hourly_pred_kw is computed)
            self._pending_save_today = True
        else:
            self._pending_save_today = False

        # hourly with per-hour kW prediction proportional to radiation share
        hourly_pred_kw = []
        for i, t in enumerate(hourly["time"]):
            day_key = t[:10]
            if day_key not in dates:
                hourly_pred_kw.append(0.0)
                continue
            day_idx = dates.index(day_key)
            day_total = pred_kwh[day_idx]
            day_rad = [hourly["shortwave_radiation"][j] or 0
                       for j, tt in enumerate(hourly["time"]) if tt.startswith(day_key)]
            day_rad_sum = sum(day_rad)
            rad = hourly["shortwave_radiation"][i] or 0
            hourly_pred_kw.append(round(day_total * rad / day_rad_sum, 3) if day_rad_sum > 0 else 0.0)

        peak_kw = max(hourly_pred_kw) if hourly_pred_kw else 0.0
        peak_idx = hourly_pred_kw.index(peak_kw) if hourly_pred_kw else 0
        peak_time = hourly["time"][peak_idx] if hourly_pred_kw else None

        # ---- actuals overlay: read today's production so far ----
        actual_today_kwh, actual_today_hourly_kwh = await self._read_today_actual_curve(
            hourly_times=hourly["time"], dates=dates,
        )

        # ---- persist today's hourly arrays to history (for past-day scrollback) ----
        if getattr(self, "_pending_save_today", False) and today_iso in dates:
            rec = self._history.get(today_iso, {})
            # Slice the hourly arrays to just today (24 entries, ordered 0..23)
            today_pred_hourly = [None] * 24
            today_actual_hourly = [None] * 24
            for j, t in enumerate(hourly["time"]):
                if not t.startswith(today_iso):
                    continue
                h = int(t[11:13])
                if 0 <= h < 24:
                    today_pred_hourly[h] = hourly_pred_kw[j]
                    if j < len(actual_today_hourly_kwh):
                        today_actual_hourly[h] = actual_today_hourly_kwh[j]
            rec["hourly_pred_kw"] = today_pred_hourly
            rec["hourly_actual_kwh"] = today_actual_hourly
            self._history[today_iso] = rec
            await self._save_storage()

        # ---- daily log of (predicted, actual, weather) for ALL stored days ----
        log_entries = []
        for d in sorted(self._history.keys()):
            rec = self._history[d]
            log_entries.append({
                "date": d,
                "predicted": rec.get("predicted"),
                "actual": rec.get("actual"),
                "ghi_wh_m2": rec.get("ghi_wh_m2"),
                "sunshine_min": rec.get("sunshine_min"),
                "cloud_pct": rec.get("cloud_pct"),
                "temp_min": rec.get("temp_min"),
                "temp_max": rec.get("temp_max"),
                "precip_mm": rec.get("precip_mm"),
                "uv_max": rec.get("uv_max"),
                "weather_code": rec.get("weather_code"),
                "hourly_pred_kw": rec.get("hourly_pred_kw"),
                "hourly_actual_kwh": rec.get("hourly_actual_kwh"),
                "throttled_minutes": rec.get("throttled_minutes"),
            })

        # ---- Optional price + throttle modules ----
        price_info = await self._read_price_info()
        throttle_info = await self._read_throttle_info()
        # Persist today's throttled-minutes so daily_log can show it later
        if throttle_info.get("minutes_today") is not None and today_iso in self._history:
            self._history[today_iso]["throttled_minutes"] = throttle_info["minutes_today"]
            await self._save_storage()

        return {
            "daily": daily,
            "hourly": hourly,
            "daily_pred_kwh": pred_kwh,
            "hourly_pred_kw": hourly_pred_kw,
            "model": self._active_model(),
            "peak_kw": round(peak_kw, 2),
            "peak_time": peak_time,
            "today_pred": pred_kwh[0] if pred_kwh else None,
            "tomorrow_pred": pred_kwh[1] if len(pred_kwh) > 1 else None,
            "week_total": round(sum(pred_kwh), 1),
            "history_days": len(self._history),
            "actual_today_kwh": actual_today_kwh,
            "actual_today_hourly_kwh": actual_today_hourly_kwh,
            "predicted_so_far_kwh": self._predicted_so_far(
                hourly_pred_kw, hourly["time"], actual_today_hourly_kwh,
            ),
            "daily_log": log_entries,
            "hourly_times": hourly["time"],
            "hourly_pred_kw_cached": hourly_pred_kw,
            "dates": dates,
            "price": price_info,
            "throttle": throttle_info,
        }

    # ---------- optional modules: price + throttle ----------
    async def _read_price_info(self) -> dict:
        """Read current sales price + hourly forecast from the configured sensor.

        Expected sensor contract (any one of these works):
          - state = current price (numeric, e.g. kr/kWh or øre/kWh).
          - attributes contain ONE of:
              * `raw_today` / `raw_tomorrow` as Nordpool-style lists of
                {start, end, value} (most common in DK setups).
              * `forecast` or `prices` as a flat list of 24 hourly values
                (chronological, starting at 00:00 today).
              * `today` / `tomorrow` as flat lists of 24 hourly values.

        Returns a dict with keys: configured, current, today (list), tomorrow
        (list), negative_hours_today (count), negative_hours_tomorrow (count),
        next_negative_window ({start, end, min_price}) or None.
        """
        cfg = self.cfg
        eid = (cfg.get(CONF_PRICE_ENTITY) or "").strip()
        if not eid:
            return {"configured": False}

        state = self.hass.states.get(eid)
        if state is None:
            return {"configured": True, "available": False}

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        current = _f(state.state)
        attrs = state.attributes or {}

        def _from_raw(arr):
            """Convert a Nordpool-style raw list -> 24 hourly floats keyed by hour."""
            out = [None] * 24
            for it in arr or []:
                start = it.get("start") or it.get("time")
                val = _f(it.get("value") if "value" in it else it.get("price"))
                if not start or val is None:
                    continue
                try:
                    h = int(str(start)[11:13])
                except (ValueError, IndexError):
                    continue
                if 0 <= h < 24:
                    out[h] = val
            return out

        def _read_day(prefix: str) -> list[float | None]:
            # Nordpool-style raw_today / raw_tomorrow
            if f"raw_{prefix}" in attrs:
                return _from_raw(attrs[f"raw_{prefix}"])
            # Flat-list variants
            for key in (prefix, f"{prefix}_prices", "forecast" if prefix == "today" else None, "prices" if prefix == "today" else None):
                if not key:
                    continue
                arr = attrs.get(key)
                if isinstance(arr, list) and len(arr) >= 24:
                    return [_f(v) for v in arr[:24]]
            return [None] * 24

        today_prices = _read_day("today")
        tomorrow_prices = _read_day("tomorrow")
        neg_today = sum(1 for v in today_prices if v is not None and v < 0)
        neg_tomorrow = sum(1 for v in tomorrow_prices if v is not None and v < 0)

        # Find next contiguous negative-price window starting from now
        now = dt_util.now()
        next_window = None
        combined = list(today_prices) + list(tomorrow_prices)  # 48 hours
        start_h = now.hour
        i = start_h
        while i < len(combined):
            v = combined[i]
            if v is not None and v < 0:
                j = i
                min_p = v
                while j < len(combined) and combined[j] is not None and combined[j] < 0:
                    min_p = min(min_p, combined[j])
                    j += 1
                # i and j are hours-from-midnight-today; convert to wall time
                base = now.replace(minute=0, second=0, microsecond=0)
                start_dt = base.replace(hour=0) + timedelta(hours=i)
                end_dt = base.replace(hour=0) + timedelta(hours=j)
                next_window = {
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "hours": j - i,
                    "min_price": round(min_p, 4),
                }
                break
            i += 1

        return {
            "configured": True,
            "available": True,
            "entity_id": eid,
            "current": current,
            "today": today_prices,
            "tomorrow": tomorrow_prices,
            "negative_hours_today": neg_today,
            "negative_hours_tomorrow": neg_tomorrow,
            "next_negative_window": next_window,
            "unit": attrs.get("unit_of_measurement"),
        }

    async def _read_throttle_info(self) -> dict:
        """Read throttle switch state + cumulative minutes-throttled today.

        Returns: configured, active (bool), minutes_today (int), since (iso or None).
        Uses the recorder to add up "on" durations within today's local window.
        """
        cfg = self.cfg
        eid = (cfg.get(CONF_THROTTLE_SWITCH) or "").strip()
        if not eid:
            return {"configured": False, "minutes_today": None}

        state = self.hass.states.get(eid)
        active = bool(state and state.state == "on")

        # Sum on-time today from the recorder
        from homeassistant.components.recorder import history, get_instance
        start = dt_util.start_of_local_day()
        end = dt_util.now()

        def _get_states():
            return history.state_changes_during_period(
                self.hass, start, end, eid, include_start_time_state=True
            )

        try:
            states_map = await get_instance(self.hass).async_add_executor_job(_get_states)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Throttle read failed: %s", err)
            states_map = {}

        entries = states_map.get(eid, [])
        total_on = timedelta()
        last_on_ts = None
        for s in entries:
            ts = dt_util.as_local(s.last_changed)
            if ts < start:
                ts = start
            if s.state == "on":
                if last_on_ts is None:
                    last_on_ts = ts
            else:
                if last_on_ts is not None:
                    total_on += ts - last_on_ts
                    last_on_ts = None
        if last_on_ts is not None:
            total_on += end - last_on_ts

        minutes_today = int(total_on.total_seconds() // 60)

        return {
            "configured": True,
            "entity_id": eid,
            "active": active,
            "minutes_today": minutes_today,
            "state": state.state if state else None,
        }

    @staticmethod
    def _predicted_so_far(hourly_pred_kw, hourly_times, actual_hourly):
        """Sum predicted kW for hours that have actual data (today, up to now).

        We treat actual_hourly[i] != None as "this hour has elapsed today".
        Each hourly bucket is 1h wide so kW ≈ kWh for that hour.
        """
        if not hourly_pred_kw or not actual_hourly:
            return None
        total = 0.0
        for i, v in enumerate(actual_hourly):
            if v is None or i >= len(hourly_pred_kw):
                continue
            total += float(hourly_pred_kw[i] or 0)
        return round(total, 2)

    async def async_refresh_actuals(self) -> None:
        """Fast refresh: re-read today's actual production curve only.

        Patches self.data in place and notifies listeners. Cheap — no API calls.
        Skips silently if the full coordinator hasn't produced data yet.
        """
        if not self.data:
            return
        hourly_times = self.data.get("hourly_times")
        dates = self.data.get("dates")
        if not hourly_times or not dates:
            return
        try:
            actual_kwh, actual_hourly = await self._read_today_actual_curve(
                hourly_times=hourly_times, dates=dates,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Fast actuals refresh failed: %s", err)
            return
        self.data["actual_today_kwh"] = actual_kwh
        self.data["actual_today_hourly_kwh"] = actual_hourly
        self.data["predicted_so_far_kwh"] = self._predicted_so_far(
            self.data.get("hourly_pred_kw_cached") or self.data.get("hourly_pred_kw"),
            hourly_times,
            actual_hourly,
        )
        # Persist today's hourly actual curve so it survives restarts and is
        # available for past-day scrollback once the day ends.
        try:
            today_iso = dt_util.now().date().isoformat()
            if today_iso in dates:
                rec = self._history.get(today_iso, {})
                today_actual_hourly = [None] * 24
                for j, t in enumerate(hourly_times):
                    if not t.startswith(today_iso):
                        continue
                    h = int(t[11:13])
                    if 0 <= h < 24 and j < len(actual_hourly):
                        today_actual_hourly[h] = actual_hourly[j]
                rec["hourly_actual_kwh"] = today_actual_hourly
                self._history[today_iso] = rec
                # Update the daily_log entry in self.data for live UI refresh
                for e in (self.data.get("daily_log") or []):
                    if e.get("date") == today_iso:
                        e["hourly_actual_kwh"] = today_actual_hourly
                        if actual_kwh is not None:
                            e["actual"] = actual_kwh
                        break
                await self._save_storage()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not persist today's hourly actual: %s", err)
        self.async_update_listeners()

    async def _read_today_actual_curve(self, hourly_times: list[str], dates: list[str]):
        """Read today's cumulative production so far + per-hour kWh up to now.

        Returns (today_total_kwh, [per-hour kWh aligned with hourly_times]).
        Hours past 'now' or on future days get None so the card can draw the
        actual line only where data exists.
        """
        cfg = self.cfg
        today = dt_util.now().date()
        today_iso = today.isoformat()
        if today_iso not in dates:
            return None, [None] * len(hourly_times)

        # Pull today's state changes for the energy entity
        daily_eid = cfg.get(CONF_DAILY_ENERGY_ENTITY)
        cumul_eid = cfg.get(CONF_PRODUCTION_ENTITY)

        try:
            from homeassistant.components.recorder import history, get_instance
        except ImportError:
            return None, [None] * len(hourly_times)

        start = datetime.combine(today, datetime.min.time(), tzinfo=dt_util.DEFAULT_TIME_ZONE)
        end = dt_util.now()

        def _get_states(eid):
            return history.state_changes_during_period(
                self.hass, start, end, eid, include_start_time_state=True, no_attributes=True,
            )

        recorder = get_instance(self.hass)
        eid = daily_eid or cumul_eid
        if not eid:
            return None, [None] * len(hourly_times)

        try:
            states = await recorder.async_add_executor_job(_get_states, eid)
        except Exception as err:
            _LOGGER.debug("Could not read today's actuals: %s", err)
            return None, [None] * len(hourly_times)

        records = []
        for s in states.get(eid, []):
            if s.state in (None, "unknown", "unavailable"):
                continue
            try:
                v = float(s.state)
            except (TypeError, ValueError):
                continue
            records.append((s.last_updated, v))

        if not records:
            return None, [None] * len(hourly_times)

        # cumulative total for the day
        if daily_eid:
            # Daily counter resets at midnight. The recorder's include_start_time_state
            # injects yesterday's final value as a phantom record at 00:00 today.
            # The CURRENT cumulative is the LAST recorded value (sorted by timestamp).
            records_sorted = sorted(records, key=lambda r: r[0])
            today_total = records_sorted[-1][1] if records_sorted else 0.0
        else:
            today_total = max(0.0, records[-1][1] - records[0][1])

        # per-hour bucket: for each hour, find the value at top-of-hour, then diff
        # Walk the records into hour buckets keyed by hour-of-day
        hour_values: dict[int, float] = {}
        for ts, v in records:
            local = dt_util.as_local(ts)
            if local.date() != today:
                continue
            h = local.hour
            # Keep latest value seen in that hour (cumulative metric → take max)
            hour_values[h] = max(v, hour_values.get(h, v))

        # convert to hourly kWh
        hourly_kwh_by_hour: dict[int, float] = {}
        if daily_eid:
            # Discard the phantom pre-reset value at hour 0: if hour 0's recorded
            # max is larger than the next hour with data, it's yesterday's residue.
            sorted_h = sorted(hour_values.keys())
            if (len(sorted_h) >= 2
                    and sorted_h[0] == 0
                    and hour_values[sorted_h[0]] > hour_values[sorted_h[1]]):
                del hour_values[sorted_h[0]]
                sorted_h = sorted_h[1:]
            # daily counter: hourly = diff between consecutive hours, clamp to >=0
            prev = 0.0
            for h in sorted_h:
                cur = hour_values[h]
                hourly_kwh_by_hour[h] = round(max(0.0, cur - prev), 2)
                prev = cur
        else:
            # cumulative lifetime counter: same delta logic
            sorted_h = sorted(hour_values.keys())
            for i, h in enumerate(sorted_h):
                if i == 0:
                    hourly_kwh_by_hour[h] = 0.0
                else:
                    prev_h = sorted_h[i - 1]
                    hourly_kwh_by_hour[h] = round(
                        max(0.0, hour_values[h] - hour_values[prev_h]), 2
                    )

        # Align to the hourly_times list (one entry per hour from Open-Meteo)
        curve = []
        current_hour = dt_util.now().hour
        for t in hourly_times:
            day_key = t[:10]
            hour = int(t[11:13])
            if day_key != today_iso:
                curve.append(None)
                continue
            if hour > current_hour:
                curve.append(None)
                continue
            curve.append(hourly_kwh_by_hour.get(hour, 0.0))

        return round(today_total, 2), curve

    # ---------- daily collection ----------
    async def async_collect_yesterday(self, date_override: str | None = None):
        """Record yesterday's actual production from the configured entity."""
        cfg = self.cfg
        target_date = (
            date.fromisoformat(date_override) if date_override
            else (dt_util.now().date() - timedelta(days=1))
        )
        iso = target_date.isoformat()

        actual = await self._read_actual_for(target_date)
        if actual is None:
            _LOGGER.debug("No actual production available for %s yet", iso)
            return

        rec = self._history.get(iso, {})
        rec["actual"] = round(actual, 2)
        # Also store the hourly actual curve so the card can render it later
        try:
            hourly = await self._read_hourly_actual_for(target_date)
            if hourly:
                rec["hourly_actual_kwh"] = hourly
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not read hourly actual for %s: %s", iso, err)
        self._history[iso] = rec
        await self._save_storage()
        _LOGGER.info("Recorded %s: actual=%.2f kWh", iso, actual)

    async def _read_hourly_actual_for(self, target: date) -> list[float | None] | None:
        """Read per-hour kWh actual production for a date. Returns list[24] or None."""
        cfg = self.cfg
        daily_eid = cfg.get(CONF_DAILY_ENERGY_ENTITY)
        cumul_eid = cfg.get(CONF_PRODUCTION_ENTITY)
        eid = daily_eid or cumul_eid
        if not eid:
            return None

        from homeassistant.components.recorder import history, get_instance
        start = datetime.combine(target, datetime.min.time(), tzinfo=dt_util.DEFAULT_TIME_ZONE)
        end = start + timedelta(days=1)

        def _get_states():
            return history.state_changes_during_period(
                self.hass, start, end, eid, include_start_time_state=True, no_attributes=True,
            )
        recorder = get_instance(self.hass)
        states = await recorder.async_add_executor_job(_get_states)

        records = []
        for s in states.get(eid, []):
            if s.state in (None, "unknown", "unavailable"):
                continue
            try:
                records.append((s.last_updated, float(s.state)))
            except (TypeError, ValueError):
                continue
        if not records:
            return None

        hour_values: dict[int, float] = {}
        for ts, v in records:
            local = dt_util.as_local(ts)
            if local.date() != target:
                continue
            h = local.hour
            hour_values[h] = max(v, hour_values.get(h, v))

        result: list[float | None] = [None] * 24
        if daily_eid:
            # Discard phantom pre-reset value at hour 0 (yesterday's residue injected
            # by recorder's include_start_time_state).
            sorted_h = sorted(hour_values.keys())
            if (len(sorted_h) >= 2
                    and sorted_h[0] == 0
                    and hour_values[sorted_h[0]] > hour_values[sorted_h[1]]):
                del hour_values[sorted_h[0]]
                sorted_h = sorted_h[1:]
            prev = 0.0
            for h in sorted_h:
                result[h] = round(max(0.0, hour_values[h] - prev), 2)
                prev = hour_values[h]
        else:
            sorted_h = sorted(hour_values.keys())
            for i, h in enumerate(sorted_h):
                if i == 0:
                    result[h] = 0.0
                else:
                    prev_h = sorted_h[i - 1]
                    result[h] = round(max(0.0, hour_values[h] - hour_values[prev_h]), 2)
        return result

    async def _read_actual_for(self, target: date) -> float | None:
        """Read actual production for a given date from the configured entity.

        Prefers a daily-energy sensor (just reads yesterday's last value).
        Falls back to the cumulative-total sensor via the recorder (difference between
        target_date 23:59 and target_date 00:00).
        """
        cfg = self.cfg
        daily_eid = cfg.get(CONF_DAILY_ENERGY_ENTITY)
        cumul_eid = cfg.get(CONF_PRODUCTION_ENTITY)

        # Use recorder to fetch historical state changes
        from homeassistant.components.recorder import history, get_instance

        start = datetime.combine(target, datetime.min.time(), tzinfo=dt_util.DEFAULT_TIME_ZONE)
        end = start + timedelta(days=1)

        def _get_states(entity_id):
            return history.state_changes_during_period(
                self.hass, start, end, entity_id, include_start_time_state=True,
                no_attributes=True,
            )

        recorder = get_instance(self.hass)

        if daily_eid:
            try:
                states = await recorder.async_add_executor_job(_get_states, daily_eid)
                vals = [float(s.state) for s in states.get(daily_eid, [])
                        if s.state not in (None, "unknown", "unavailable")]
                if vals:
                    return max(vals)
            except Exception as e:
                _LOGGER.warning("Failed reading daily entity %s: %s", daily_eid, e)

        if cumul_eid:
            try:
                states = await recorder.async_add_executor_job(_get_states, cumul_eid)
                vals = [float(s.state) for s in states.get(cumul_eid, [])
                        if s.state not in (None, "unknown", "unavailable")]
                if len(vals) >= 2:
                    return max(0.0, vals[-1] - vals[0])
            except Exception as e:
                _LOGGER.warning("Failed reading cumulative entity %s: %s", cumul_eid, e)

        return None

    # ---------- refit ----------
    def should_refit(self) -> bool:
        if not self._last_refit:
            return len(self._history) >= MIN_TRAINING_DAYS
        try:
            last = date.fromisoformat(self._last_refit)
        except ValueError:
            return True
        days_since = (dt_util.now().date() - last).days
        return days_since >= int(self.cfg.get(CONF_REFIT_DAYS, DEFAULT_REFIT_DAYS))

    async def async_refit_model(self, force: bool = False):
        """Refit linear model (kWh = a + b * GHI) from history.

        We need both actual and a GHI value per day. For days where we don't have
        stored GHI we pull from Open-Meteo archive in one batch.
        """
        usable = {d: r for d, r in self._history.items() if "actual" in r}
        if len(usable) < MIN_TRAINING_DAYS and not force:
            _LOGGER.info("Refit skipped: only %d usable days (< %d)", len(usable), MIN_TRAINING_DAYS)
            return

        # Backfill missing GHI / weather aggregates from Open-Meteo archive
        WEATHER_FIELDS = ("ghi_wh_m2", "cloud_pct", "sunshine_min", "temp_max")
        missing = sorted(
            d for d, r in self._history.items()
            if any(r.get(f) is None for f in WEATHER_FIELDS)
        )
        if missing:
            await self._backfill_ghi(missing)
            usable = {d: r for d, r in self._history.items() if "actual" in r}

        pairs = [(r["ghi_wh_m2"], r["actual"]) for r in usable.values()
                 if r.get("ghi_wh_m2") is not None]
        if len(pairs) < MIN_TRAINING_DAYS:
            _LOGGER.warning("Not enough (ghi, actual) pairs after backfill: %d", len(pairs))
            return

        # OLS: y = a + b x
        n = len(pairs)
        sx = sum(p[0] for p in pairs); sy = sum(p[1] for p in pairs)
        sxx = sum(p[0]*p[0] for p in pairs); sxy = sum(p[0]*p[1] for p in pairs)
        denom = n * sxx - sx * sx
        if denom == 0:
            _LOGGER.warning("Refit denominator zero; aborting")
            return
        b = (n * sxy - sx * sy) / denom
        a = (sy - b * sx) / n

        # Residuals + outlier removal (exclude points > 1.3x RMSE — likely throttle/snow)
        resids = [(y - (a + b * x)) for x, y in pairs]
        rmse = (sum(r*r for r in resids) / n) ** 0.5
        if rmse > 0:
            keep = [(x, y) for (x, y), r in zip(pairs, resids) if abs(r) <= RESID_THRESHOLD_RMSE_MULT * rmse]
            if len(keep) >= MIN_TRAINING_DAYS:
                pairs = keep
                n = len(pairs)
                sx = sum(p[0] for p in pairs); sy = sum(p[1] for p in pairs)
                sxx = sum(p[0]*p[0] for p in pairs); sxy = sum(p[0]*p[1] for p in pairs)
                denom = n * sxx - sx * sx
                b = (n * sxy - sx * sy) / denom
                a = (sy - b * sx) / n
                resids = [(y - (a + b * x)) for x, y in pairs]
                rmse = (sum(r*r for r in resids) / n) ** 0.5

        mean_y = sum(p[1] for p in pairs) / n
        ss_tot = sum((p[1] - mean_y)**2 for p in pairs)
        r2 = 1 - sum(r*r for r in resids) / ss_tot if ss_tot > 0 else 0.0

        self._model = {
            "intercept": round(a, 4),
            "slope": round(b, 6),
            "rmse": round(rmse, 2),
            "r2": round(r2, 4),
            "trained_on": n,
            "trained_at": dt_util.now().date().isoformat(),
        }
        self._last_refit = dt_util.now().date().isoformat()
        await self._save_storage()
        _LOGGER.info("Refit complete: a=%.3f b=%.6f RMSE=%.2f R²=%.3f on n=%d",
                     a, b, rmse, r2, n)
        await self.async_request_refresh()

    async def _backfill_ghi(self, dates: list[str]):
        """Fetch GHI + weather aggregates archive in one batch."""
        if not dates:
            return
        cfg = self.cfg
        from .const import OPENMETEO_ARCHIVE
        params = {
            "latitude": cfg[CONF_LATITUDE],
            "longitude": cfg[CONF_LONGITUDE],
            "start_date": min(dates),
            "end_date": max(dates),
            "daily": ",".join([
                "shortwave_radiation_sum", "sunshine_duration",
                "cloud_cover_mean", "temperature_2m_max", "temperature_2m_min",
                "precipitation_sum", "uv_index_max", "weather_code",
            ]),
            "timezone": "auto",
        }
        session = async_get_clientsession(self.hass)
        try:
            async with asyncio.timeout(30):
                async with session.get(OPENMETEO_ARCHIVE, params=params) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
        except Exception as e:
            _LOGGER.warning("Archive backfill failed: %s", e)
            return
        d = data.get("daily", {})
        times = d.get("time", [])
        ghis = d.get("shortwave_radiation_sum", [])
        sds = d.get("sunshine_duration", [])
        clouds = d.get("cloud_cover_mean", [])
        tmaxs = d.get("temperature_2m_max", [])
        tmins = d.get("temperature_2m_min", [])
        precips = d.get("precipitation_sum", [])
        uvs = d.get("uv_index_max", [])
        codes = d.get("weather_code", [])
        def _at(arr, i):
            return arr[i] if i < len(arr) else None
        for i, t in enumerate(times):
            rec = self._history.get(t, {})
            g = _at(ghis, i)
            if g is not None:
                rec["ghi_wh_m2"] = round(g * MJ_TO_WH, 1)
            sd = _at(sds, i)
            if sd is not None:
                rec["sunshine_min"] = round(sd / 60)
            for key, arr in (
                ("cloud_pct", clouds), ("temp_max", tmaxs), ("temp_min", tmins),
                ("precip_mm", precips), ("uv_max", uvs), ("weather_code", codes),
            ):
                v = _at(arr, i)
                if v is not None:
                    rec[key] = v
            self._history[t] = rec
        await self._save_storage()

    async def async_import_history(self, history_list: list[dict]):
        """Bulk-import historical (date, actual_kwh) pairs."""
        count = 0
        for entry in history_list:
            d = entry.get("date")
            a = entry.get("actual_kwh")
            if not d or a is None:
                continue
            rec = self._history.get(d, {})
            rec["actual"] = float(a)
            self._history[d] = rec
            count += 1
        await self._save_storage()
        _LOGGER.info("Imported %d historical days", count)
        # Backfill weather + fit a model on whatever we got
        await self._backfill_ghi(sorted(rec_date for rec_date in self._history.keys()))
        await self.async_refit_model(force=True)

    async def async_bootstrap_from_recorder(self) -> dict:
        """One-shot: scan HA recorder for past production, backfill weather, refit.

        Walks `CONF_BOOTSTRAP_DAYS` days back, reads daily-total kWh from the
        configured production sensor, pulls matching GHI from the Open-Meteo
        archive, then fits the linear model. Safe to call repeatedly; will be
        skipped on subsequent calls via `self._bootstrap_done`.
        """
        if self._bootstrap_done:
            _LOGGER.debug("Bootstrap already complete; skipping")
            return self._bootstrap_summary or {"skipped": True}
        cfg = self.cfg
        if cfg.get(CONF_SENSOR_KIND, "none") == "none":
            _LOGGER.info("Bootstrap skipped: no production sensor configured")
            self._bootstrap_done = True
            self._bootstrap_summary = {"skipped": "no_sensor"}
            await self._save_storage()
            return self._bootstrap_summary

        days_back = int(cfg.get(CONF_BOOTSTRAP_DAYS, DEFAULT_BOOTSTRAP_DAYS))
        today = dt_util.now().date()
        new_days = 0
        for offset in range(1, days_back + 1):
            target = today - timedelta(days=offset)
            iso = target.isoformat()
            # Skip days we already have a recorded actual for
            if self._history.get(iso, {}).get("actual") is not None:
                continue
            try:
                actual = await self._read_actual_for(target)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Bootstrap: read failed for %s: %s", iso, err)
                continue
            if actual is None or actual <= 0:
                continue
            rec = self._history.get(iso, {})
            rec["actual"] = round(float(actual), 2)
            self._history[iso] = rec
            new_days += 1

        _LOGGER.info("Bootstrap: collected %d new actuals from recorder", new_days)

        # Batch-backfill GHI for any day missing it. The archive endpoint
        # accepts a date range, so one call covers the lot.
        missing_ghi = [d for d, r in self._history.items() if r.get("ghi_wh_m2") is None]
        if missing_ghi:
            try:
                await self._backfill_ghi(sorted(missing_ghi))
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Bootstrap: GHI backfill failed: %s", err)

        # Fit the model on whatever we have
        try:
            await self.async_refit_model(force=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Bootstrap: refit failed: %s", err)

        self._bootstrap_done = True
        self._bootstrap_summary = {
            "days_collected": new_days,
            "total_history_days": len(self._history),
            "model": self._model,
        }
        await self._save_storage()
        await self.async_request_refresh()
        return self._bootstrap_summary

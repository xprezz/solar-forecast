"""Sensor platform for Solar Forecast."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    sensors = [
        SolarForecastDailyKwh(coordinator, entry, 0, "Today"),
        SolarForecastDailyKwh(coordinator, entry, 1, "Tomorrow"),
        SolarForecastWeekTotal(coordinator, entry),
        SolarForecastPeakPower(coordinator, entry),
        SolarForecastStrategy(coordinator, entry, 0, "Strategy Today"),
        SolarForecastStrategy(coordinator, entry, 1, "Strategy Tomorrow"),
        SolarForecastModelInfo(coordinator, entry),
        SolarForecastHourly(coordinator, entry),
        SolarForecastTodayActual(coordinator, entry),
        SolarForecastDailyLog(coordinator, entry),
    ]
    async_add_entities(sensors)


class _BaseSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Solar Forecast",
            model="Linear weather model",
            sw_version="1.0.0",
        )


class SolarForecastDailyKwh(_BaseSensor):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator, entry, idx: int, label: str):
        super().__init__(coordinator, entry)
        self._idx = idx
        self._attr_name = label
        self._attr_unique_id = f"{entry.entry_id}_pred_d{idx}"

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        preds = data.get("daily_pred_kwh") or []
        return preds[self._idx] if self._idx < len(preds) else None

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        daily = data.get("daily") or {}
        if self._idx >= len(daily.get("time", [])):
            return {}
        return {
            "date": daily["time"][self._idx],
            "ghi_mj_m2": daily["shortwave_radiation_sum"][self._idx],
            "sunshine_min": round((daily["sunshine_duration"][self._idx] or 0) / 60),
            "cloud_pct": daily["cloud_cover_mean"][self._idx],
            "temp_min": daily["temperature_2m_min"][self._idx],
            "temp_max": daily["temperature_2m_max"][self._idx],
            "precip_mm": daily["precipitation_sum"][self._idx],
            "uv_max": daily["uv_index_max"][self._idx],
            "weather_code": daily["weather_code"][self._idx],
            "sunrise": daily["sunrise"][self._idx],
            "sunset": daily["sunset"][self._idx],
        }


class SolarForecastWeekTotal(_BaseSensor):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:solar-power-variant"
    _attr_name = "7 Day Total"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_pred_week"

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("week_total")

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        daily = data.get("daily") or {}
        times = daily.get("time", [])
        preds = data.get("daily_pred_kwh") or []

        def _at(key, i):
            arr = daily.get(key) or []
            return arr[i] if i < len(arr) else None

        per_day_weather = []
        for i, d in enumerate(times):
            sd = _at("sunshine_duration", i)
            per_day_weather.append({
                "date": d,
                "predicted_kwh": preds[i] if i < len(preds) else None,
                "ghi_mj_m2": _at("shortwave_radiation_sum", i),
                "sunshine_min": round(sd / 60) if sd is not None else None,
                "cloud_pct": _at("cloud_cover_mean", i),
                "temp_min": _at("temperature_2m_min", i),
                "temp_max": _at("temperature_2m_max", i),
                "precip_mm": _at("precipitation_sum", i),
                "uv_max": _at("uv_index_max", i),
                "weather_code": _at("weather_code", i),
            })

        return {
            "per_day_kwh": dict(zip(times, preds)),
            "per_day_weather": per_day_weather,
        }


class SolarForecastPeakPower(_BaseSensor):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_icon = "mdi:flash"
    _attr_name = "Forecast Peak Power"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_peak"

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("peak_kw")

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        return {"peak_time": data.get("peak_time")}


class SolarForecastStrategy(_BaseSensor):
    _attr_icon = "mdi:lightbulb-on-outline"

    def __init__(self, coordinator, entry, idx: int, label: str):
        super().__init__(coordinator, entry)
        self._idx = idx
        self._attr_name = label
        self._attr_unique_id = f"{entry.entry_id}_strategy_d{idx}"

    @property
    def native_value(self):
        strat = (self.coordinator.data or {}).get("strategy") or []
        if self._idx < len(strat):
            return strat[self._idx]["class"]
        return None

    @property
    def extra_state_attributes(self):
        strat = (self.coordinator.data or {}).get("strategy") or []
        if self._idx >= len(strat):
            return {}
        s = strat[self._idx]
        return {
            "date": s["date"],
            "predicted_kwh": s["predicted_kwh"],
            "tips": s["tips"],
            "allocation": s["allocation"],
            "cloud_pct": s.get("cloud_pct"),
            "precip_mm": s.get("precip_mm"),
            "temp_min": s.get("temp_min"),
            "temp_max": s.get("temp_max"),
            "weather_code": s.get("weather_code"),
        }


class SolarForecastModelInfo(_BaseSensor):
    _attr_icon = "mdi:chart-bell-curve-cumulative"
    _attr_name = "Model RMSE"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_model_rmse"

    @property
    def native_value(self):
        m = (self.coordinator.data or {}).get("model") or {}
        return m.get("rmse")

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        m = data.get("model") or {}
        return {
            "intercept": m.get("intercept"),
            "slope": m.get("slope"),
            "r2": m.get("r2"),
            "trained_on_days": m.get("trained_on"),
            "trained_at": m.get("trained_at"),
            "history_days_collected": data.get("history_days"),
        }


class SolarForecastHourly(_BaseSensor):
    _attr_icon = "mdi:chart-line"
    _attr_name = "Hourly Forecast"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_hourly"

    @property
    def native_value(self):
        # state = today's peak kW for a tidy summary
        return (self.coordinator.data or {}).get("peak_kw")

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        hourly = data.get("hourly") or {}
        return {
            "time": hourly.get("time"),
            "pred_kw": data.get("hourly_pred_kw"),
            "actual_kwh": data.get("actual_today_hourly_kwh"),
            "radiation": hourly.get("shortwave_radiation"),
            "cloud_cover": hourly.get("cloud_cover"),
            "temperature": hourly.get("temperature_2m"),
            "precipitation": hourly.get("precipitation"),
            "wind_speed": hourly.get("wind_speed_10m"),
        }


class SolarForecastTodayActual(_BaseSensor):
    """Today's actual production so far, from the configured energy entity."""
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:solar-power-variant-outline"
    _attr_name = "Today Actual"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_today_actual"

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("actual_today_kwh")

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        pred_today = data.get("today_pred")
        pred_so_far = data.get("predicted_so_far_kwh")
        actual = data.get("actual_today_kwh")
        delta_pct = None
        # Compare actual-so-far vs predicted-so-far (apples to apples).
        if pred_so_far and actual is not None and pred_so_far > 0:
            delta_pct = round((actual - pred_so_far) / pred_so_far * 100, 1)
        return {
            "predicted_today_kwh": pred_today,
            "predicted_so_far_kwh": pred_so_far,
            "delta_pct_vs_predicted": delta_pct,
        }


class SolarForecastDailyLog(_BaseSensor):
    """Rolling daily log of (predicted, actual) for charting / external analysis."""
    _attr_icon = "mdi:database-clock-outline"
    _attr_name = "Daily Log"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_daily_log"

    @property
    def native_value(self):
        # Sensor state = number of logged days (helpful glance value)
        return (self.coordinator.data or {}).get("history_days")

    @property
    def extra_state_attributes(self):
        from .const import ACCURACY_CUTOFF_DATE
        data = self.coordinator.data or {}
        log = data.get("daily_log") or []
        # Compute accuracy for the last 14 days where both exist AND date >= cutoff
        cutoff_entries = [r for r in log if r.get("date", "") >= ACCURACY_CUTOFF_DATE]
        last14 = [r for r in cutoff_entries[-14:] if r.get("predicted") is not None and r.get("actual") is not None]
        mape = None
        if last14:
            mape = round(sum(
                abs(r["actual"] - r["predicted"]) / max(0.1, r["actual"]) for r in last14
            ) / len(last14) * 100, 1)
        return {
            "entries": log,
            "days_logged": len(log),
            "mape_pct_last_14d": mape,
        }

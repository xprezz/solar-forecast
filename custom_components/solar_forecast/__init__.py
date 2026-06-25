"""Solar Forecast integration: weather-driven PV forecast with self-recalibration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import event as ev_helpers

from .const import (
    DOMAIN, PLATFORMS, CONF_REFIT_DAYS, DEFAULT_REFIT_DAYS,
    CONF_PRODUCTION_ENTITY, CONF_DAILY_ENERGY_ENTITY,
)
from .coordinator import SolarForecastCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Solar Forecast from a config entry."""
    coordinator = SolarForecastCoordinator(hass, entry)
    await coordinator.async_load_storage()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Daily collection: every day at 00:05 local, record yesterday's actual production
    async def _daily_collect(now):
        await coordinator.async_collect_yesterday()
        # Periodic refit
        if coordinator.should_refit():
            await coordinator.async_refit_model()

    entry.async_on_unload(
        ev_helpers.async_track_time_change(hass, _daily_collect, hour=0, minute=5, second=0)
    )

    # Periodic forecast refresh every 3 hours (in addition to the coordinator's own)
    async def _periodic_refresh(now):
        await coordinator.async_request_refresh()

    entry.async_on_unload(
        ev_helpers.async_track_time_interval(hass, _periodic_refresh, timedelta(hours=3))
    )

    # Fast actuals refresh every 5 minutes: re-reads recorder, no API calls.
    # NOTE: do NOT subscribe to live state changes on the inverter — Solis pushes
    # every few seconds and each refresh runs a recorder query, which pegged CPU.
    async def _fast_actuals(now):
        await coordinator.async_refresh_actuals()

    entry.async_on_unload(
        ev_helpers.async_track_time_interval(hass, _fast_actuals, timedelta(minutes=5))
    )

    # Register services
    async def _svc_refit(call: ServiceCall):
        await coordinator.async_refit_model(force=True)

    async def _svc_collect(call: ServiceCall):
        # allow passing 'date' to backfill a specific day
        date_str = call.data.get("date")
        await coordinator.async_collect_yesterday(date_override=date_str)

    async def _svc_import(call: ServiceCall):
            # Import historical (date, actual_kwh) pairs from a list provided in service
            # data, OR from a JSON file on disk (default: /config/bootstrap_history.json).
            history = call.data.get("history")
            if not history:
                import json
                from pathlib import Path
                path = Path(call.data.get("file") or hass.config.path("bootstrap_history.json"))
                if not path.exists():
                    _LOGGER.error("import_history: no 'history' provided and %s not found", path)
                    return
                try:
                    history = await hass.async_add_executor_job(
                        lambda: json.loads(path.read_text())
                    )
                    _LOGGER.info("import_history: loaded %d records from %s", len(history), path)
                except Exception as err:
                    _LOGGER.error("import_history: failed to read %s: %s", path, err)
                    return
            await coordinator.async_import_history(history)

    async def _svc_backfill_hourly(call: ServiceCall):
        days = int(call.data.get("days", 30))
        n = await coordinator.async_backfill_hourly_actuals(days_back=days)
        if n:
            await coordinator.async_request_refresh()
        _LOGGER.info("backfill_hourly_actuals: filled %d days", n)

    hass.services.async_register(DOMAIN, "refit", _svc_refit)
    hass.services.async_register(DOMAIN, "collect", _svc_collect)
    hass.services.async_register(DOMAIN, "import_history", _svc_import)
    hass.services.async_register(DOMAIN, "backfill_hourly_actuals", _svc_backfill_hourly)

    # Run a one-shot hourly-actuals backfill shortly after startup so the card
    # has past-day curves for recent days that pre-dated the hourly storage.
    async def _startup_backfill(now):
        try:
            n = await coordinator.async_backfill_hourly_actuals(days_back=30)
            if n:
                await coordinator.async_request_refresh()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Startup hourly backfill failed: %s", err)

    entry.async_on_unload(
        ev_helpers.async_call_later(hass, 30, _startup_backfill)
    )

    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok

"""Config flow for Solar Forecast.

Two-step flow:
  1. Name + location + (optional) production sensor with sensor-kind toggle.
  2. Panel specs (kWp, tilt, azimuth) — only shown when no production sensor
     is provided, since with real data we'll learn the effective shape.
"""
from __future__ import annotations

from typing import Any
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_LATITUDE, CONF_LONGITUDE,
    CONF_PRODUCTION_ENTITY, CONF_DAILY_ENERGY_ENTITY, CONF_SENSOR_KIND,
    CONF_PANEL_KWP, CONF_TILT, CONF_AZIMUTH,
    CONF_REFIT_DAYS, CONF_BOOTSTRAP_DAYS,
    CONF_PRICE_ENTITY, CONF_THROTTLE_SWITCH,
    DEFAULT_PANEL_KWP, DEFAULT_TILT, DEFAULT_AZIMUTH,
    DEFAULT_REFIT_DAYS, DEFAULT_BOOTSTRAP_DAYS,
)


SENSOR_KIND_OPTIONS = [
    selector.SelectOptionDict(value="none", label="No production sensor (use panel specs)"),
    selector.SelectOptionDict(value="cumulative", label="Cumulative lifetime kWh (e.g. inverter total)"),
    selector.SelectOptionDict(value="daily", label="Daily kWh (resets at midnight)"),
]


def _step1_schema(hass, defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema({
        vol.Required(CONF_NAME, default=d.get(CONF_NAME, "Solar Forecast")): str,
        vol.Required(CONF_LATITUDE, default=d.get(CONF_LATITUDE, hass.config.latitude)): vol.Coerce(float),
        vol.Required(CONF_LONGITUDE, default=d.get(CONF_LONGITUDE, hass.config.longitude)): vol.Coerce(float),
        vol.Required(CONF_SENSOR_KIND, default=d.get(CONF_SENSOR_KIND, "none")): selector.SelectSelector(
            selector.SelectSelectorConfig(options=SENSOR_KIND_OPTIONS, mode=selector.SelectSelectorMode.LIST)
        ),
        vol.Optional(CONF_PRODUCTION_ENTITY, default=d.get(CONF_PRODUCTION_ENTITY, "")): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        vol.Optional(CONF_PRICE_ENTITY, default=d.get(CONF_PRICE_ENTITY, "")): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        vol.Optional(CONF_THROTTLE_SWITCH, default=d.get(CONF_THROTTLE_SWITCH, "")): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["switch", "binary_sensor", "input_boolean"])
        ),
    })


def _step2_schema(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema({
        vol.Required(CONF_PANEL_KWP, default=d.get(CONF_PANEL_KWP, DEFAULT_PANEL_KWP)): vol.All(
            vol.Coerce(float), vol.Range(min=0.1, max=200.0)
        ),
        vol.Required(CONF_TILT, default=d.get(CONF_TILT, DEFAULT_TILT)): vol.All(
            vol.Coerce(float), vol.Range(min=0.0, max=90.0)
        ),
        vol.Required(CONF_AZIMUTH, default=d.get(CONF_AZIMUTH, DEFAULT_AZIMUTH)): vol.All(
            vol.Coerce(float), vol.Range(min=0.0, max=360.0)
        ),
    })


def _advanced_schema(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema({
        vol.Required(CONF_REFIT_DAYS, default=d.get(CONF_REFIT_DAYS, DEFAULT_REFIT_DAYS)): vol.All(
            int, vol.Range(min=1, max=90)
        ),
        vol.Required(CONF_BOOTSTRAP_DAYS, default=d.get(CONF_BOOTSTRAP_DAYS, DEFAULT_BOOTSTRAP_DAYS)): vol.All(
            int, vol.Range(min=30, max=1825)
        ),
        vol.Optional(CONF_PRICE_ENTITY, default=d.get(CONF_PRICE_ENTITY, "")): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        vol.Optional(CONF_THROTTLE_SWITCH, default=d.get(CONF_THROTTLE_SWITCH, "")): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["switch", "binary_sensor", "input_boolean"])
        ),
    })


class SolarForecastConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self):
        self._step1: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            kind = user_input.get(CONF_SENSOR_KIND, "none")
            ent = (user_input.get(CONF_PRODUCTION_ENTITY) or "").strip()
            if kind != "none" and not ent:
                errors["base"] = "sensor_required"
            else:
                # Normalise sensor slot based on selected kind
                if kind == "cumulative":
                    user_input[CONF_PRODUCTION_ENTITY] = ent
                    user_input[CONF_DAILY_ENERGY_ENTITY] = ""
                elif kind == "daily":
                    user_input[CONF_DAILY_ENERGY_ENTITY] = ent
                    user_input[CONF_PRODUCTION_ENTITY] = ""
                else:
                    user_input[CONF_PRODUCTION_ENTITY] = ""
                    user_input[CONF_DAILY_ENERGY_ENTITY] = ""
                self._step1 = user_input
                # Branch: no sensor → panel specs required; with sensor → finish
                if kind == "none":
                    return await self.async_step_panel()
                return await self._finish()
        return self.async_show_form(
            step_id="user",
            data_schema=_step1_schema(self.hass),
            errors=errors,
            description_placeholders={"docs": "https://github.com/xprezz/solar-forecast"},
        )

    async def async_step_panel(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._step1.update(user_input)
            return await self._finish()
        return self.async_show_form(step_id="panel", data_schema=_step2_schema())

    async def _finish(self):
        data = self._step1
        await self.async_set_unique_id(f"{data[CONF_LATITUDE]:.4f},{data[CONF_LONGITUDE]:.4f}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=data[CONF_NAME], data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SolarForecastOptionsFlow()


class SolarForecastOptionsFlow(config_entries.OptionsFlow):
    """HA auto-injects `self.config_entry`; do not set it manually (deprecation -> error)."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        defaults = {**self.config_entry.data, **self.config_entry.options}
        # Show panel specs only if no sensor configured; otherwise just refit/bootstrap settings
        kind = defaults.get(CONF_SENSOR_KIND, "none")
        if kind == "none":
            schema = _step2_schema(defaults).extend(_advanced_schema(defaults).schema)
        else:
            schema = _advanced_schema(defaults)
        return self.async_show_form(step_id="init", data_schema=schema)


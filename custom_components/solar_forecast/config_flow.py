"""Config flow for Solar Forecast."""
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
    CONF_PRODUCTION_ENTITY, CONF_DAILY_ENERGY_ENTITY,
    CONF_HOME_KWH, CONF_CAR_KWH, CONF_BATTERY_KWH,
    CONF_REFIT_DAYS, CONF_NEG_PRICE_ENTITY,
    DEFAULT_HOME_KWH, DEFAULT_CAR_KWH, DEFAULT_BATTERY_KWH, DEFAULT_REFIT_DAYS,
)


def _schema(hass, defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema({
        vol.Required(CONF_NAME, default=d.get(CONF_NAME, "Solar Forecast")): str,
        vol.Required(CONF_LATITUDE, default=d.get(CONF_LATITUDE, hass.config.latitude)): vol.Coerce(float),
        vol.Required(CONF_LONGITUDE, default=d.get(CONF_LONGITUDE, hass.config.longitude)): vol.Coerce(float),
        vol.Optional(CONF_PRODUCTION_ENTITY, default=d.get(CONF_PRODUCTION_ENTITY, "")): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        vol.Optional(CONF_DAILY_ENERGY_ENTITY, default=d.get(CONF_DAILY_ENERGY_ENTITY, "")): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        vol.Optional(CONF_NEG_PRICE_ENTITY, default=d.get(CONF_NEG_PRICE_ENTITY, "")): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["binary_sensor", "switch", "input_boolean"])
        ),
        vol.Required(CONF_HOME_KWH, default=d.get(CONF_HOME_KWH, DEFAULT_HOME_KWH)): vol.Coerce(float),
        vol.Required(CONF_CAR_KWH, default=d.get(CONF_CAR_KWH, DEFAULT_CAR_KWH)): vol.Coerce(float),
        vol.Required(CONF_BATTERY_KWH, default=d.get(CONF_BATTERY_KWH, DEFAULT_BATTERY_KWH)): vol.Coerce(float),
        vol.Required(CONF_REFIT_DAYS, default=d.get(CONF_REFIT_DAYS, DEFAULT_REFIT_DAYS)): vol.All(int, vol.Range(min=1, max=90)),
    })


class SolarForecastConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            # Single instance per (lat, lon)
            await self.async_set_unique_id(f"{user_input[CONF_LATITUDE]:.4f},{user_input[CONF_LONGITUDE]:.4f}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema(self.hass), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SolarForecastOptionsFlow(config_entry)


class SolarForecastOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        defaults = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(self.hass, defaults))

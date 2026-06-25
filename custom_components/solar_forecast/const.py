"""Constants for the Solar Forecast integration."""
DOMAIN = "solar_forecast"
PLATFORMS = ["sensor"]

# config keys
CONF_LATITUDE = "latitude"
CONF_LONGITUDE = "longitude"
CONF_PRODUCTION_ENTITY = "production_entity"   # cumulative kWh sensor (preferred)
CONF_DAILY_ENERGY_ENTITY = "daily_energy_entity"  # optional daily kWh entity
CONF_HOME_KWH = "home_kwh"
CONF_CAR_KWH = "car_kwh"
CONF_BATTERY_KWH = "battery_kwh"
CONF_REFIT_DAYS = "refit_interval_days"
CONF_NEG_PRICE_ENTITY = "negative_price_entity"  # optional binary_sensor

DEFAULT_HOME_KWH = 15.0
DEFAULT_CAR_KWH = 10.0
DEFAULT_BATTERY_KWH = 25.0
DEFAULT_REFIT_DAYS = 7

# Open-Meteo
OPENMETEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPENMETEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
MJ_TO_WH = 277.7778

# storage
STORAGE_VERSION = 1
STORAGE_KEY = "solar_forecast.history"

# refit settings
MIN_TRAINING_DAYS = 30
RESID_THRESHOLD_RMSE_MULT = 1.3  # exclude days where |resid| > mult * baseline rmse

# accuracy calculation
ACCURACY_CUTOFF_DATE = "2025-06-22"  # only calculate accuracy from this date forward

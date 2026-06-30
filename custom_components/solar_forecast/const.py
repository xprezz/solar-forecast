"""Constants for the Solar Forecast integration."""
DOMAIN = "solar_forecast"
PLATFORMS = ["sensor"]

# ---- config keys ----
CONF_LATITUDE = "latitude"
CONF_LONGITUDE = "longitude"
CONF_PRODUCTION_ENTITY = "production_entity"        # cumulative kWh sensor (preferred)
CONF_DAILY_ENERGY_ENTITY = "daily_energy_entity"    # daily-reset kWh sensor
CONF_SENSOR_KIND = "sensor_kind"                    # "none" | "cumulative" | "daily"
CONF_PANEL_KWP = "panel_kwp"                        # nameplate kWp (sum of all strings)
CONF_TILT = "tilt"                                  # 0=flat, 90=vertical
CONF_AZIMUTH = "azimuth"                            # 0=N, 90=E, 180=S, 270=W
CONF_REFIT_DAYS = "refit_interval_days"
CONF_BOOTSTRAP_DAYS = "bootstrap_days"              # how far back to scan recorder on first run

# ---- defaults ----
DEFAULT_TILT = 30.0       # typical Northern-hemisphere roof
DEFAULT_AZIMUTH = 180.0   # south
DEFAULT_PANEL_KWP = 6.0   # ~average residential install
DEFAULT_REFIT_DAYS = 7
DEFAULT_BOOTSTRAP_DAYS = 365

# ---- Open-Meteo ----
OPENMETEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPENMETEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
MJ_TO_WH = 277.7778

# ---- storage ----
STORAGE_VERSION = 1
STORAGE_KEY = "solar_forecast.history"

# ---- refit settings ----
MIN_TRAINING_DAYS = 30
RESID_THRESHOLD_RMSE_MULT = 1.3

# ---- physics fallback ----
# kWh_day ≈ kWp × GHI(Wh/m²) / 1000 × PV_DERATING × tilt_azimuth_factor
PV_DERATING = 0.78  # typical losses (inverter, soiling, temp, wiring)


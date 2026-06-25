# Solar Forecast

Home Assistant integration that turns Open‑Meteo radiation forecasts into a
self‑recalibrating daily PV production prediction, using your own inverter
history to keep the model honest.

## Features

- 7‑day daily kWh forecast + hourly kW curve (Open‑Meteo)
- Compares predicted vs actual every day; rolling MAPE accuracy metric
- Automatic linear refit against your own history (no fixed kWp guess)
- Surplus / good / modest / low “strategy” classes per day with tips
- Stores hourly predicted + actual curves for past days (scrollback in the card)
- Backfills weather + hourly actuals on startup; manual service available
- Negative‑price awareness (optional binary_sensor input)

Designed to be paired with the
[`solar-forecast-card`](https://github.com/xprezz/solar-forecast-card)
Lovelace card, but works fine with the plain HA dashboard too.

## Install via HACS

1. HACS → Integrations → ⋮ → **Custom repositories**
2. Add `https://github.com/xprezz/solar-forecast` as category **Integration**
3. Install **Solar Forecast**
4. Restart Home Assistant
5. Settings → Devices & Services → **Add Integration** → *Solar Forecast*

## Configuration

In the config flow:

| Field | Meaning |
| --- | --- |
| Latitude / Longitude | Your site (defaults to HA home) |
| Production entity | Your inverter's cumulative kWh sensor (preferred) |
| Daily energy entity | Optional daily kWh sensor |
| Home / Car / Battery kWh | Sizing for the strategy classifier |
| Refit interval (days) | How often the linear model is re‑fit (default 7) |
| Negative price entity | Optional binary_sensor — flips card to “avoid running” mode |

## Sensors created

- `sensor.<name>_today`, `sensor.<name>_tomorrow` – daily kWh predicted
- `sensor.<name>_7_day_total` – weekly outlook + `per_day_kwh` / `per_day_weather`
- `sensor.<name>_forecast_peak_power` – kW peak today
- `sensor.<name>_strategy_today`, `sensor.<name>_strategy_tomorrow`
- `sensor.<name>_model_rmse` – current fit quality
- `sensor.<name>_hourly_forecast` – 7‑day hourly arrays
- `sensor.<name>_today_actual` – production so far today vs prediction
- `sensor.<name>_daily_log` – full historical log + 14‑day MAPE (since cutoff)

## Services

- `solar_forecast.refit` – force a model refit from current history
- `solar_forecast.collect_yesterday` – manually capture yesterday's actual
- `solar_forecast.import_history` – import a CSV / JSON daily history
- `solar_forecast.backfill_hourly_actuals` – rebuild past hourly curves from recorder

## Accuracy

MAPE is computed only from days where both a prediction and an actual exist
*and* the date is on or after the cutoff (default `2025‑06‑22`), so that
imported historical actuals without corresponding predictions don't artificially
deflate the metric.

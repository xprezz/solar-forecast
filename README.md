# Solar Forecast

A Home Assistant integration that turns [Open-Meteo](https://open-meteo.com/)
radiation forecasts into a self-calibrating daily PV production prediction.

If you have a production sensor with several months of history it trains a
linear model on **your** inverter, refines it weekly, and tells you each day
how far the forecast was off. If you don't, it ships a physics-based fallback
based on panel kWp + tilt + azimuth so you get something useful on day one.

Pairs with [`solar-forecast-card`](https://github.com/xprezz/solar-forecast-card)
for the dashboard, but works fine with the built-in sensor cards too.

## Features

- 7-day daily kWh + hourly kW forecast (Open-Meteo)
- Forecast accuracy tracking (rolling 7-day MAPE, last-complete days only)
- Three onboarding paths — see below
- Automatic weekly refit against your accumulated history
- Hourly actuals stored for past days so the card can scrollback
- Zero personal/household assumptions — purely forecast + accuracy

## Install via HACS

1. HACS → Integrations → ⋮ → **Custom repositories**
2. Add `https://github.com/xprezz/solar-forecast` as category **Integration**
3. Install **Solar Forecast**
4. Restart Home Assistant
5. Settings → Devices & Services → **Add Integration** → *Solar Forecast*

## Onboarding — pick whichever fits

The setup wizard asks one question first: **do you have a PV production sensor?**

### Option A — “No sensor”, physics fallback
Enter your total installed kWp, panel tilt and azimuth. The integration uses
a simplified physics model (`kWh ≈ kWp × GHI × derating × tilt_az_factor`)
to predict production. Forecast accuracy is naturally limited but it works
from minute one, and you can add a sensor later in the options.

### Option B — Cumulative kWh sensor (lifetime counter)
Point the integration at your inverter's lifetime energy sensor (`solis_pv_total_energy_generation`, `growatt_total_energy`, etc).
60 seconds after setup it will scan Home Assistant's recorder for the past
`bootstrap_days` (default 365) of daily totals, pull matching radiation from
the Open-Meteo archive, and fit your personal model.

You do **not** need to enter panel specs — the regression learns the
effective shape, which is the right answer for multi-string / multi-azimuth
installs where a single tilt/azimuth pair can't represent reality.

### Option C — Daily kWh sensor (resets each midnight)
Same as B but for sensors that reset to 0 at midnight. The recorder bootstrap
pairs each day's *peak* value with that day's radiation total.

### Option D — Bulk import a CSV / JSON
If you have spreadsheet data from before Home Assistant was recording, call:

```yaml
service: solar_forecast.import_history
data:
  history:
    - { date: "2024-01-01", actual_kwh: 12.3 }
    - { date: "2024-01-02", actual_kwh: 18.7 }
    # ...
```

Or drop a JSON file at `/config/bootstrap_history.json` and call the service
with no arguments. A refit fires automatically.

## Optional opt-in modules (v2.1+)

These activate **only when you configure the matching entity** — leave them blank to keep the integration forecast-only.

### Sales price sensor

Point at any sensor whose **state** is the current sales price (e.g. `sensor.solar_real_sales_price`, a Nordpool sensor, an Energi Data Service sensor). The integration looks for the hourly forecast in the sensor's attributes, accepting any of:

- `raw_today` / `raw_tomorrow` — Nordpool-style list of `{start, end, value}` (most common in DK).
- `today` / `tomorrow` — flat list of 24 hourly values, chronological from 00:00.
- `forecast` / `prices` — same flat-list format.

When configured, you get `sensor.<name>_negative_price_window` with the next contiguous negative-price window (state = hour count, attributes = start/end/min price) and a banner in the card.

### Production throttle switch

A `switch` / `binary_sensor` / `input_boolean` that is **ON** when your inverter is being curtailed (e.g. by your own negative-price automation). The integration tallies on-time using the HA recorder and exposes `sensor.<name>_throttled_minutes_today`, plus a banner in the card so it's clear *why* today's actual is lower than the prediction.

## Services

| Service | What it does |
|---|---|
| `solar_forecast.refit` | Force-refit the model from current history |
| `solar_forecast.collect` | Record a day's actual production (defaults to yesterday) |
| `solar_forecast.import_history` | Bulk import historical `(date, actual_kwh)` pairs |
| `solar_forecast.bootstrap` | Re-run the recorder scan + refit |
| `solar_forecast.backfill_hourly_actuals` | Fill the per-hour curve for past days |

## Sensors

| Entity | Purpose |
|---|---|
| `sensor.<name>_today` | Predicted kWh today |
| `sensor.<name>_tomorrow` | Predicted kWh tomorrow |
| `sensor.<name>_7_day_total` | Weekly forecast total |
| `sensor.<name>_forecast_peak_power` | Peak instantaneous kW (& time) over the next 7 days |
| `sensor.<name>_today_actual` | Cumulative actuals so far today |
| `sensor.<name>_hourly_forecast` | 168-hour predicted + actual arrays (chart fuel) |
| `sensor.<name>_daily_log` | Rolling log of (predicted, actual) + 7-day MAPE |
| `sensor.<name>_model_rmse` | Current model RMSE / R² / training-day count |

## Notes

- The model retrains automatically every `refit_interval_days` (default 7)
  using ordinary least-squares with an inline 1.3× RMSE outlier filter that
  drops snow-day / grid-throttle anomalies.
- Today's value is intentionally excluded from the rolling MAPE — partial-day
  actuals create huge spurious morning errors otherwise.
- Forecast updates: every 3 hours. Actuals re-read: every 5 minutes.

## License

MIT

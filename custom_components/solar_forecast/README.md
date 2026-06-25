# Solar Forecast — HACS package

Drop these folders into your `home-assistant-config` repo at the same level as
`configuration.yaml`:

```
custom_components/solar_forecast/      # the integration
www/community/solar-forecast-card/     # the Lovelace card
hacs.json                              # for HACS discoverability
```

Then:
1. Commit and push.
2. Restart Home Assistant.
3. *Settings → Devices & Services → Add Integration → Solar Forecast*.
4. Add `/local/community/solar-forecast-card/solar-forecast-card.js` as a
   Lovelace JS Module resource.
5. (Optional) Run the `solar_forecast.import_history` service with the contents
   of `bootstrap_history.json` to pre-load the 382-day Copenhagen calibration
   set as your starting history.

See [custom_components/solar_forecast/README.md](custom_components/solar_forecast/README.md)
for full details.

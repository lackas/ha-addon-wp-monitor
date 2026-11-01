# Changelog

## 0.6.7 (2026-11-01)

- The battery module values (`sensor.vx3_akku_modul_*`) are read at startup too, in the same single call as the other rarely changing VX3 values. The VX3 sends them only when they change, so after a restart of the app they were missing until then, and Spook offered to delete their long-term statistics.

## 0.6.6 (2026-11-01)

- States are sent again as soon as HA is back after a restart, and every 15 minutes on a timer. The minute check before missed slots, so rarely changing values like `sensor.vx3_akku_ausnahme` stayed missing after an HA restart.

## 0.6.5 (2026-11-01)

- The VX3 subscription also covers its `EEBUS` and `gateway` devices, to see what the gridBox does when the battery control mode jumps. Their records carry a `device` field; records of device 0 stay as they were. Only device 0 goes to the HA states.

## 0.6.4 (2026-10-31)

- Weekly sums of the compressor per mode: `sensor.wp_woche_waerme_heizen`, `_waerme_warmwasser`, `_strom_heizen`, `_strom_warmwasser` (kWh, `total_increasing`, reset every Monday 00:00) and `sensor.wp_woche_az_heizen` / `_warmwasser`. The heat pump does not push them reliably.
- The heat pump poll is a single call with the API's `regex` filter, for the current power and the weekly sums alike.

## 0.6.3 (2026-10-31)

- `sensor.vx3_einspeisegrenze`, `sensor.vx3_verbrauchsgrenze` and their `_status` sensors from `device.power.statusReport.production` and `.consumption`, the limits the gridBox writes over EEBUS. Read once at startup like the battery state, then from the WebSocket stream.

## 0.6.2 (2026-10-30)

- Read `ess.battery.state` once at startup. The VX3 sends it only when it changes, so `sensor.vx3_akku_ausnahme` stayed empty for hours after a restart.

## 0.6.1 (2026-10-30)

- `sensor.vx3_akku_ausnahme` from `ess.battery.state` replaces `binary_sensor.vx3_akku_sleep_mode`, which the VX3 hardly ever sends. Shows `noException`, `batterySleepMode`, `batteryDeepDischargeRecharge` or a combination.

## 0.6.0 (2026-10-30)

- Mirrors values that only the app client (scope Internal) gets into Home Assistant states through the Supervisor API: `sensor.wp_live_waermeleistung`, `sensor.wp_live_stromleistung` and `sensor.wp_live_cop` for the heat pump compressor, `sensor.vx3_akku_modul_{0..5}_soh`, `_innenwiderstand` and `_zyklen` per battery module and `binary_sensor.vx3_akku_sleep_mode`.
- The heat pump gateway sends compressor power only on subscribe, so it is polled every `wp_poll_s` seconds (new option, default 300, 0 disables), two API calls each time. The battery values come from the existing WebSocket stream.
- HA forgets these states on restart, so they are sent again every 15 minutes.

## 0.5.1 (2026-10-17)

- Daily report mails through `notify.send_message` on the SMTP notify entity (`MAIL_ENTITY`, default `notify.mail_lackas_gmail_com`) instead of the legacy `notify.mail` action, which HA removes in 2027.3.

## 0.5.0 (2026-10-17)

- Hourly `collect.py`: copies the HA history of about 35 heat pump, energy and weather sensors into `/share/wp-monitor/data.db`, because HA itself keeps only 10 days.
- Daily report at 06:10 (`wp_daily_report.py`) for the previous day, sent through the HA service `notify.mail`. A marker in `/data` prevents a second mail after a restart.
- Both jobs ran on a laptop before. They use the Supervisor API now (`homeassistant_api: true`) and run as separate processes, so a crash does not touch the loggers.

## 0.4.1 (2026-06-24)

- Log the running addon version on startup (`WP Monitor vX.Y.Z starting`), read from `config.yaml` (now copied into the image). Makes it obvious in the log which version is actually live.

## 0.4.0 (2026-06-24)

- Auto-recover stalled WebSocket threads: the heartbeat now restarts a Viessmann WS thread that is *alive but delivering no new events* (e.g. stuck in the OAuth/subscription retry loop), not only threads that have died. Controlled by new option `ws_stale_restart_s` (default 300s, 0 disables). The old thread is signalled to stop cleanly first to avoid a duplicate writer. Fixes silent multi-day gaps in `viessmann_*.jsonl`.

## 0.3.0 (2026-06-06)

- Weekly log rotation: `shelly_brunnen.csv` and `viessmann_*.jsonl` rotate at the ISO-week boundary (Mon 00:00 local) and are gzipped to `<file>.YYYY-WNN.gz` in the same directory. Archives are kept indefinitely.

## 0.1.3 (2026-04-29)

- Fix `token_invalid` lockup: trigger `renewToken()` (fresh email/password login) when WS subscription fails with token error, instead of retrying forever

## 0.1.2 (2026-04-23)

- Watchdog heartbeat every 60s with row/event counters and file sizes
- Shared counters for Shelly rows and Viessmann events

## 0.1.1 (2026-04-23)

- Use `python:3.12-alpine` directly instead of HA base image (skip s6-overlay)

## 0.1.0 (2026-04-23)

- Initial release
- Shelly Pro 3EM 1 Hz polling (CSV)
- Viessmann Cloud WebSocket listener (JSONL)
- Main-thread watchdog restarts dead worker threads
- Output to `/share/wp-monitor/` (Samba-accessible)

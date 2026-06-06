# Changelog

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

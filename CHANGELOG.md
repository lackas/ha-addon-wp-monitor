# Changelog

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

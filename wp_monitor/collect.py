"""Collect HA sensor data into local SQLite database.

Pulls history from HA REST API and stores in data.db.
Designed to run periodically (e.g., hourly via cron) to accumulate
history beyond HA's ~10 day retention window.

Runs hourly inside the addon. Outside of it, point HA_URL, HA_TOKEN_PATH and
WP_DB at your own setup.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import requests
from pathlib import Path

# --- HA connection ---
# Im Addon spricht das Script ueber den Supervisor-Proxy mit HA.
HA_URL = os.environ.get("HA_URL", "http://supervisor/core")
TOKEN_FILE = os.environ.get("HA_TOKEN_PATH")
DB_PATH = Path(os.environ.get("WP_DB", "/share/wp-monitor/data.db"))

# --- Sensors to collect ---
# (entity_id, fetch_attributes, state_map)
# fetch_attributes: if True, store attributes instead of/in addition to state
# state_map: dict to map string states to numeric values
SENSORS = [
    ("sensor.cu401b_g_compressor_modulation", False, None),
    ("sensor.cu401b_g_primary_circuit_supply_temperature", False, None),
    ("sensor.cu401b_g_primary_circuit_return_temperature", False, None),
    ("sensor.cu401b_g_supply_temperature", False, None),
    ("sensor.cu401b_g_return_temperature", False, None),
    ("sensor.cu401b_g_buffer_main_temperature", False, None),
    ("water_heater.cu401b_g_domestic_hot_water", True, None),
    ("binary_sensor.cu401b_g_dhw_charging", False, {"on": 1, "off": 0}),
    ("sensor.cu401b_g_compressor_phase", False, {"standby": 0, "ready": 1, "preparing": 2, "heating": 3, "cooling": 4, "pause": 5, "off": -1}),
    ("sensor.cu401b_g_compressor_starts", False, None),
    ("sensor.cu401b_g_compressor_hours", False, None),
    ("sensor.cu401b_g_primary_circuit_pump_rotation", False, None),
    ("sensor.cu401b_g_secondary_circuit_supply_temperature", False, None),
    # Kaeltekreis: im Stillstand zeigt der Druck die kaelteste Stelle, siehe
    # docs/brunnenpumpe-bereitschaft.md
    ("sensor.cu401b_g_evaporator_liquid_temperature", False, None),
    ("sensor.cu401b_g_compressor_inlet_temperature", False, None),
    ("sensor.cu401b_g_compressor_outlet_temperature", False, None),
    ("sensor.cu401b_g_compressor_inlet_pressure", False, None),
    ("sensor.cu401b_g_pressure", False, None),
    ("sensor.cu401b_g_pressure_2", False, None),
    ("sensor.brunnenpumpe_stromverbrauch", False, None),
    ("sensor.gridx_heat_pump_power", False, None),
    # Enum laut gridX-API: UNKNOWN/OFF/AUTO/RECOMMEND_ON/ON. Die frueheren Keys
    # LOCKED/RECOMMENDED/FORCED gab es nie, unbekannte States werden verworfen,
    # deshalb standen bis 11.09.2026 nur AUTO-Zeilen in der DB.
    ("sensor.gridx_heat_pump_sg_ready_state", False,
     {"AUTO": 0, "OFF": 1, "RECOMMEND_ON": 2, "ON": 3, "UNKNOWN": -1}),
    ("sensor.tasmota_sml_power_curr", False, None),
    # Gridbox cumulative energy counters (for ROI model)
    ("sensor.e3_vitocharge_06_pv_energy_production_total", False, None),
    ("sensor.e3_vitocharge_06_energy_import_from_grid", False, None),
    ("sensor.e3_vitocharge_06_energy_export_to_grid", False, None),
    ("sensor.tasmota_sml_total_in", False, None),
    # PV Forecast (Forecast.Solar) — 4 planes
    ("sensor.energy_production_today", False, None),        # VX3 East
    ("sensor.energy_production_today_2", False, None),      # VX3 West
    ("sensor.energy_production_today_3", False, None),      # SMA East
    ("sensor.energy_production_today_4", False, None),      # SMA West
    # Weather
    ("sensor.openweathermap_cloud_coverage", False, None),
    ("sensor.openweathermap_temperature", False, None),
    ("sensor.cu401b_g_outside_temperature", False, None),
]


def _headers():
    token = Path(TOKEN_FILE).read_text().strip() if TOKEN_FILE else os.environ["SUPERVISOR_TOKEN"]
    return {"Authorization": f"Bearer {token}"}


def init_db(db_path):
    """Create database and tables if they don't exist."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sensor_data (
            entity_id TEXT NOT NULL,
            time TEXT NOT NULL,
            state REAL,
            attribute TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (entity_id, time, attribute)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_entity_time
        ON sensor_data(entity_id, time)
    """)
    conn.commit()
    return conn


def get_last_time(conn, entity_id):
    """Get the most recent timestamp for an entity in the DB."""
    row = conn.execute(
        "SELECT MAX(time) FROM sensor_data WHERE entity_id = ?",
        (entity_id,),
    ).fetchone()
    if row[0]:
        return datetime.fromisoformat(row[0])
    return None


def fetch_history(entity_id, start, end=None, with_attributes=False):
    """Fetch state history from HA REST API."""
    params = {"filter_entity_id": entity_id, "minimal_response": ""}
    if not with_attributes:
        params["no_attributes"] = ""

    path = f"/api/history/period/{start.isoformat()}"
    if end:
        params["end_time"] = end.isoformat()

    r = requests.get(f"{HA_URL}{path}", headers=_headers(), params=params, timeout=60)
    r.raise_for_status()
    data = r.json()

    if not data or not data[0]:
        return []
    return data[0]


def collect_sensor(conn, entity_id, fetch_attrs, state_map):
    """Collect data for one sensor and insert into DB."""
    now = datetime.now(timezone.utc)
    last = get_last_time(conn, entity_id)

    if last:
        # Overlap by 1h for dedup safety
        start = last - timedelta(hours=1)
    else:
        # No data yet — fetch max HA retention (~10 days)
        start = now - timedelta(days=10)

    records = fetch_history(entity_id, start, now, with_attributes=fetch_attrs)
    if not records:
        return 0

    rows = []
    for entry in records:
        time_str = entry.get("last_changed", entry.get("last_updated", ""))
        if not time_str:
            continue

        # Parse and normalize to UTC ISO format
        t = datetime.fromisoformat(time_str).astimezone(timezone.utc)
        time_iso = t.isoformat()

        if fetch_attrs:
            # water_heater: extract attributes
            attrs = entry.get("attributes", {})
            for attr_name in ("current_temperature", "temperature"):
                val = attrs.get(attr_name)
                if val is not None:
                    try:
                        rows.append((entity_id, time_iso, float(val), attr_name))
                    except (ValueError, TypeError):
                        pass
            # Also store the state (e.g., "idle", "heat")
            state = entry.get("state", "")
            state_map_wh = {"idle": 0, "off": 0, "heat": 1, "heating": 1}
            mapped = state_map_wh.get(state)
            if mapped is not None:
                rows.append((entity_id, time_iso, float(mapped), "state"))
        else:
            state = entry.get("state", "")
            if state_map:
                val = state_map.get(state)
                if val is None:
                    continue
                val = float(val)
            else:
                try:
                    val = float(state)
                except (ValueError, TypeError):
                    continue
            rows.append((entity_id, time_iso, val, ""))

    if not rows:
        return 0

    conn.executemany(
        "INSERT OR IGNORE INTO sensor_data (entity_id, time, state, attribute) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn.total_changes


def main():
    conn = init_db(DB_PATH)
    total_new = 0

    print(f"Collecting sensor data → {DB_PATH}")
    print(f"{'Entity':<55} {'Fetched':>8} {'New':>8}")
    print("-" * 75)

    for entity_id, fetch_attrs, state_map in SENSORS:
        # Count before
        before = conn.execute(
            "SELECT COUNT(*) FROM sensor_data WHERE entity_id = ?",
            (entity_id,),
        ).fetchone()[0]

        try:
            collect_sensor(conn, entity_id, fetch_attrs, state_map)
        except Exception as e:
            print(f"{entity_id:<55} {'ERROR':>8} {str(e)[:20]:>8}")
            continue

        after = conn.execute(
            "SELECT COUNT(*) FROM sensor_data WHERE entity_id = ?",
            (entity_id,),
        ).fetchone()[0]

        new = after - before
        total_new += new

        # Get fetch count from last operation
        last = get_last_time(conn, entity_id)
        last_str = last.strftime("%m-%d %H:%M") if last else "—"
        print(f"{entity_id:<55} {after:>8} {new:>+8}  (latest: {last_str})")

    # DB stats
    total_rows = conn.execute("SELECT COUNT(*) FROM sensor_data").fetchone()[0]
    db_size = DB_PATH.stat().st_size / 1024 / 1024
    print("-" * 75)
    print(f"Total: {total_rows} rows, {db_size:.1f} MB, {total_new} new rows")

    conn.close()


if __name__ == "__main__":
    main()

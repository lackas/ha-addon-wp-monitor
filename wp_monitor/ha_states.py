"""Mirror selected ViCare features into Home Assistant states.

The values exist only with the app client (scope Internal), which the ViCare
integration cannot use. States go through the Supervisor API, so they need no
setup in HA, but they are gone after an HA restart until the next update;
republish() covers that.
"""

import logging
import os
import queue
import re
import threading
import time

import requests

log = logging.getLogger("wp_monitor")

HA_URL = os.environ.get("HA_URL", "http://supervisor/core")
WP_POLL_FEATURES = (
    "heating.compressors.0.heat.production.current",
    "heating.compressors.0.power.consumption.current",
)

_queue = queue.Queue()
_published = {}  # entity_id -> (state, attributes)
_raw = {}  # values derived states are computed from


def _attrs(name, unit=None, device_class=None, state_class="measurement", icon=None):
    attrs = {"friendly_name": name, "state_class": state_class}
    if unit:
        attrs["unit_of_measurement"] = unit
    if device_class:
        attrs["device_class"] = device_class
    if icon:
        attrs["icon"] = icon
    return attrs


def _cop():
    heat, power = _raw.get("heat"), _raw.get("power")
    # Below 50 W the compressor is off and the ratio means nothing.
    state = round(heat / power, 2) if heat is not None and power and power > 50 else "unknown"
    return "sensor.wp_live_cop", state, _attrs("WP Live-COP", icon="mdi:heat-pump")


def states_for(feature, vals):
    """Return (entity_id, state, attributes) for one feature event."""
    value = vals.get("value")
    if feature == "heating.compressors.0.heat.production.current":
        _raw["heat"] = value
        return [
            ("sensor.wp_live_waermeleistung", value, _attrs("WP Wärmeleistung", "W", "power")),
            _cop(),
        ]
    if feature == "heating.compressors.0.power.consumption.current":
        _raw["power"] = round(value * 1000) if value is not None else None
        return [
            ("sensor.wp_live_stromleistung", _raw["power"], _attrs("WP Stromleistung", "W", "power")),
            _cop(),
        ]
    m = re.fullmatch(r"ess\.battery\.(\d+)\.information", feature)
    if m:
        n = m.group(1)
        resistance = vals.get("internalBatteryResistance")
        return [
            (f"sensor.vx3_akku_modul_{n}_soh", vals.get("stateOfHealth"),
             _attrs(f"VX3 Akku Modul {n} SoH", "%", icon="mdi:battery-heart-variant")),
            (f"sensor.vx3_akku_modul_{n}_innenwiderstand",
             round(resistance * 1000, 1) if resistance is not None else None,
             _attrs(f"VX3 Akku Modul {n} Innenwiderstand", "mΩ", icon="mdi:omega")),
            (f"sensor.vx3_akku_modul_{n}_zyklen", vals.get("cycleDischarge"),
             _attrs(f"VX3 Akku Modul {n} Zyklen", state_class="total_increasing", icon="mdi:battery-sync")),
        ]
    if feature == "ess.features.batterySleepMode" and "active" in vals:
        return [("binary_sensor.vx3_akku_sleep_mode", "on" if vals.get("active") else "off",
                 {"friendly_name": "VX3 Akku Sleep-Mode", "icon": "mdi:sleep"})]
    return []


def publish(feature, vals):
    """Queue changed states; called from the WebSocket callbacks, so it must not block."""
    for entity_id, state, attrs in states_for(feature, vals):
        if state is None:
            continue
        if _published.get(entity_id) != (state, attrs):
            _published[entity_id] = (state, attrs)
            _queue.put(entity_id)


def republish():
    """Send everything again, HA forgets these states on restart."""
    for entity_id in list(_published):
        _queue.put(entity_id)


def _sender():
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}"
    failed_at = 0
    while True:
        entity_id = _queue.get()
        state, attrs = _published[entity_id]
        try:
            r = session.post(f"{HA_URL}/api/states/{entity_id}",
                             json={"state": state, "attributes": attrs}, timeout=10)
            r.raise_for_status()
        except Exception as e:
            # One line per 10 minutes is enough while HA restarts.
            if time.time() - failed_at > 600:
                failed_at = time.time()
                log.warning("HA state %s failed: %s", entity_id, e)


def start_sender():
    threading.Thread(target=_sender, name="ha-states", daemon=True).start()


def wp_poll_thread(login, gateway_id, interval):
    """The heat pump gateway sends compressor power only on subscribe, so poll it.

    The server refreshes these values about once a minute."""
    mgr = installation = None
    while True:
        try:
            if mgr is None:
                mgr = login()
            if installation is None:
                installation = mgr.get("/equipment/installations")["data"][0]["id"]
            for feature in WP_POLL_FEATURES:
                data = mgr.get(f"/features/installations/{installation}/gateways/{gateway_id}"
                               f"/devices/0/features/{feature}")["data"]
                vals = {k: v.get("value") for k, v in data["properties"].items()}
                publish(feature, vals)
        except Exception as e:
            log.warning("WP poll failed: %s", e)
        time.sleep(interval)

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
from urllib.parse import quote

import requests

log = logging.getLogger("wp_monitor")

HA_URL = os.environ.get("HA_URL", "http://supervisor/core")
# One call for the compressor's current power and the weekly sums per mode.
WP_POLL_REGEX = r"heating\.compressors\.0\.(heat\.production|power\.consumption)\..*"

# The VX3 sends these only when they change, which can take hours or days.
ONCE_VX_FEATURES = (
    "ess.battery.state",
    "device.power.statusReport.production",
    "device.power.statusReport.consumption",
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
    m = re.fullmatch(r"heating\.compressors\.0\.(heat\.production|power\.consumption)\.(heating|dhw)\.week", feature)
    if m:
        kind = "waerme" if m.group(1) == "heat.production" else "strom"
        mode, label = ("heizen", "Heizen") if m.group(2) == "heating" else ("warmwasser", "Warmwasser")
        _raw[(kind, mode)] = value
        heat, power = _raw.get(("waerme", mode)), _raw.get(("strom", mode))
        # Early in the week the sums are too small for a meaningful ratio.
        az = round(heat / power, 2) if heat is not None and power and power >= 0.5 else "unknown"
        return [
            (f"sensor.wp_woche_{kind}_{mode}", value,
             _attrs(f"WP {'Wärme' if kind == 'waerme' else 'Strom'} {label} Woche", "kWh", "energy",
                    "total_increasing")),
            (f"sensor.wp_woche_az_{mode}", az, _attrs(f"WP Arbeitszahl {label} Woche", icon="mdi:heat-pump")),
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
    # Tells a standby from batterySleepMode apart from the pccControlMode hang,
    # which runs with noException, see docs/vx3-akku-pcc-modus.md in ha-config.
    if feature == "ess.battery.state" and vals.get("exception"):
        exceptions = sorted(e for e in vals["exception"] if e != "noException")
        return [("sensor.vx3_akku_ausnahme", ", ".join(exceptions) or "noException",
                 {"friendly_name": "VX3 Akku Ausnahme", "icon": "mdi:battery-alert-variant-outline"})]
    # Who sets pccControlMode is open; the gridBox writes these limits over EEBUS,
    # so their history next to sensor.vx3_akku_steuermodus may show it.
    for name, label, entity in (("production", "Einspeisegrenze", "einspeisegrenze"),
                                ("consumption", "Verbrauchsgrenze", "verbrauchsgrenze")):
        if feature == f"device.power.statusReport.{name}" and "status" in vals:
            return [
                (f"sensor.vx3_{entity}_status", vals["status"],
                 {"friendly_name": f"VX3 {label} Status", "icon": "mdi:transmission-tower"}),
                (f"sensor.vx3_{entity}", vals.get("limit"), _attrs(f"VX3 {label}", "W", "power")),
            ]
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


def wp_poll_thread(login, wp_gateway, vx_gateway, interval):
    """The heat pump gateway sends compressor power only on subscribe, so poll it.

    The server refreshes these values about once a minute. ONCE_VX_FEATURES are
    read once at startup."""
    mgr = installation = None
    once = [(vx_gateway, f) for f in ONCE_VX_FEATURES] if vx_gateway else []
    while True:
        try:
            if mgr is None:
                mgr = login()
            if installation is None:
                installation = mgr.get("/equipment/installations")["data"][0]["id"]
            base = f"/features/installations/{installation}/gateways"
            for gateway, feature in once:
                data = mgr.get(f"{base}/{gateway}/devices/0/features/{feature}")["data"]
                publish(feature, {k: v.get("value") for k, v in data["properties"].items()})
            once = []
            for data in mgr.get(f"{base}/{wp_gateway}/devices/0/features?regex={quote(WP_POLL_REGEX)}")["data"]:
                publish(data["feature"], {k: v.get("value") for k, v in data["properties"].items()})
        except Exception as e:
            log.warning("WP poll failed: %s", e)
        time.sleep(interval)

#!/usr/bin/env python3
"""WP Monitor — Shelly 3EM + Viessmann WebSocket logger for HA addon."""

import json
import logging
import os
import threading
import time
from datetime import datetime
from urllib.error import URLError
from urllib.request import urlopen

import requests
import socketio
from PyViCareLive.PyViCareOAuthManager import ViCareOAuthManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wp_monitor")

OUTPUT_DIR = "/share/wp-monitor"
SHELLY_CSV = os.path.join(OUTPUT_DIR, "shelly_brunnen.csv")
VIESSMANN_JSONL = os.path.join(OUTPUT_DIR, "viessmann_ws.jsonl")
CSV_HEADER = "ts,total_w,a_w,b_w,c_w,a_a,b_a,c_a,a_v,b_v,c_v\n"

WS_URL = "https://api.viessmann-climatesolutions.com/live-updates/v1/iot"
WS_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

# Shared counters for watchdog heartbeat
shelly_count = 0
viessmann_count = 0


def load_options():
    with open("/data/options.json") as f:
        return json.load(f)


def setup_output_dir():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if not os.path.exists(SHELLY_CSV) or os.path.getsize(SHELLY_CSV) == 0:
        with open(SHELLY_CSV, "w") as f:
            f.write(CSV_HEADER)


def shelly_thread(ip, poll_hz):
    global shelly_count
    url = f"http://{ip}/rpc/EM.GetStatus?id=0"
    interval = 1.0 / poll_hz
    f = open(SHELLY_CSV, "a", buffering=1)
    next_tick = time.time()
    while True:
        ts = datetime.now().isoformat(timespec="milliseconds")
        try:
            with urlopen(url, timeout=2) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            row = (
                f"{ts}"
                f",{d.get('total_act_power', '')}"
                f",{d.get('a_act_power', '')}"
                f",{d.get('b_act_power', '')}"
                f",{d.get('c_act_power', '')}"
                f",{d.get('a_current', '')}"
                f",{d.get('b_current', '')}"
                f",{d.get('c_current', '')}"
                f",{d.get('a_voltage', '')}"
                f",{d.get('b_voltage', '')}"
                f",{d.get('c_voltage', '')}\n"
            )
            f.write(row)
            shelly_count += 1
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            log.warning("Shelly poll error: %s", e)
        next_tick += interval
        sleep_for = next_tick - time.time()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            next_tick = time.time()


def viessmann_thread(email, password, client_id, gateway_id, watchdog_timeout):
    subs = {"subscriptions": [
        {"id": "0", "type": "device-features", "gatewayId": gateway_id, "version": "2"},
    ]}
    global viessmann_count
    mgr = ViCareOAuthManager(email, password, client_id, "/data/token.save")
    f = open(VIESSMANN_JSONL, "a", buffering=1)

    while True:
        try:
            resp = mgr.oauth_session.post(WS_URL, json.dumps(subs), headers=WS_HEADERS, timeout=30)
            if resp.status_code not in (200, 201):
                log.error("WS subscription failed: %d %s", resp.status_code, resp.text[:200])
                time.sleep(30)
                continue
            sub = resp.json()
            log.info("WS subscription OK, namespace=%s", sub["namespace"])
        except Exception as e:
            log.error("WS subscription error: %s", e)
            err_str = str(e).lower()
            if "token_invalid" in err_str or "invalid_token" in err_str or "expired" in err_str:
                try:
                    mgr.renewToken()
                    log.info("Token re-authenticated after invalid token error")
                except Exception as renew_err:
                    log.error("Token renewal failed: %s", renew_err)
            time.sleep(30)
            continue

        ns = sub["namespace"]
        sio = socketio.Client(reconnection=False)
        last_msg_time = [time.time()]
        stop_watchdog = threading.Event()

        def watchdog():
            while not stop_watchdog.is_set():
                if stop_watchdog.wait(30):
                    return
                silence = time.time() - last_msg_time[0]
                if silence > watchdog_timeout:
                    log.warning("WS watchdog: silent %ds, disconnecting", int(silence))
                    try:
                        sio.disconnect()
                    except Exception:
                        pass
                    return

        @sio.on("connect", namespace=ns)
        def on_connect():
            log.info("WS connected")

        @sio.on("disconnect", namespace=ns)
        def on_disconnect():
            log.info("WS disconnected after %d events", viessmann_count)

        @sio.on("feature", namespace=ns)
        def feature_changed(data):
            global viessmann_count
            viessmann_count += 1
            last_msg_time[0] = time.time()
            feat = data.get("feature", {})
            name = feat.get("feature", "?")
            props = feat.get("properties", {})
            vals = {k: v.get("value", v) for k, v in props.items() if isinstance(v, dict)}
            ts = datetime.now().isoformat(timespec="milliseconds")
            record = {"ts": ts, "feature": name, "values": vals}
            f.write(json.dumps(record, separators=(",", ":")) + "\n")

        log.info("WS connecting to %s", sub["url"])
        try:
            sio.connect(sub["url"], transports=["websocket"],
                        socketio_path=sub["path"], namespaces=ns)
        except Exception as e:
            log.error("WS connection failed: %s", e)
            time.sleep(30)
            continue

        wd = threading.Thread(target=watchdog, daemon=True)
        wd.start()
        try:
            sio.wait()
        except Exception as e:
            log.warning("WS wait error: %s", e)
        finally:
            stop_watchdog.set()

        log.info("WS reconnecting in 30s...")
        time.sleep(30)
        try:
            mgr.renewToken()
            log.info("WS token renewed")
        except Exception as e:
            log.warning("WS token renewal failed: %s, retry in 60s", e)
            time.sleep(60)


def main():
    opts = load_options()
    setup_output_dir()

    shelly_ip = opts.get("shelly_ip", "192.168.1.136")
    shelly_hz = opts.get("shelly_poll_hz", 1)
    ws_timeout = opts.get("watchdog_timeout_s", 120)

    log.info("Starting WP Monitor — Shelly %s @ %d Hz", shelly_ip, shelly_hz)

    t_shelly = threading.Thread(target=shelly_thread, args=(shelly_ip, shelly_hz),
                                name="shelly", daemon=True)
    t_shelly.start()

    t_viessmann = None
    email = opts.get("vicare_email")
    password = opts.get("vicare_password")
    client_id = opts.get("vicare_client_id")
    gateway_id = opts.get("vicare_gateway_id")
    if email and password:
        log.info("Starting Viessmann WS logger for gateway %s", gateway_id)
        t_viessmann = threading.Thread(
            target=viessmann_thread,
            args=(email, password, client_id, gateway_id, ws_timeout),
            name="viessmann", daemon=True)
        t_viessmann.start()
    else:
        log.warning("ViCare credentials not configured, WS logger disabled")

    log.info("Watchdog active — heartbeat every 60s")
    while True:
        time.sleep(60)

        # Heartbeat
        shelly_ok = "alive" if t_shelly.is_alive() else "DEAD"
        ws_ok = "alive" if (t_viessmann and t_viessmann.is_alive()) else ("DEAD" if t_viessmann else "off")
        csv_size = os.path.getsize(SHELLY_CSV) if os.path.exists(SHELLY_CSV) else 0
        jsonl_size = os.path.getsize(VIESSMANN_JSONL) if os.path.exists(VIESSMANN_JSONL) else 0
        log.info("Heartbeat: shelly=%s (%d rows), viessmann=%s (%d events), "
                 "csv=%.1fMB, jsonl=%.1fMB",
                 shelly_ok, shelly_count, ws_ok, viessmann_count,
                 csv_size / 1048576, jsonl_size / 1048576)

        if not t_shelly.is_alive():
            log.error("Shelly thread died — restarting")
            t_shelly = threading.Thread(target=shelly_thread, args=(shelly_ip, shelly_hz),
                                        name="shelly", daemon=True)
            t_shelly.start()
        if t_viessmann and not t_viessmann.is_alive():
            log.error("Viessmann thread died — restarting")
            t_viessmann = threading.Thread(
                target=viessmann_thread,
                args=(email, password, client_id, gateway_id, ws_timeout),
                name="viessmann", daemon=True)
            t_viessmann.start()


if __name__ == "__main__":
    main()

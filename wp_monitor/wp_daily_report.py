"""Tagesreport für Viessmann CU401B: Warmwasser, Heizen, Brunnenpumpe, Störungen.

Energie kommt aus den Wochenzählern der WP (Strom und Wärme je für WW und Heizen,
Reset montags). errorCode 6c = Regelhochdruck, nicht der Strömungswächter,
Codetabelle docs/wp-fehlercodes.md.

Läuft täglich um 06:10 im Addon und verschickt über den HA-Dienst notify.mail.
Außerhalb davon WP_SHARE, HA_URL und HA_TOKEN_PATH setzen, --dry-run verschickt nichts.
"""
import bisect
import csv
import gzip
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

SHARE = Path(os.environ.get("WP_SHARE", "/share/wp-monitor"))
WP_JSONL = SHARE / "viessmann_wp.jsonl"
SHELLY_CSV = SHARE / "shelly_brunnen.csv"

PUMP_W = 380  # Brunnenpumpe, gemessen
HA_URL = os.environ.get("HA_URL", "http://supervisor/core")
MAIL_SERVICE = os.environ.get("MAIL_SERVICE", "notify/mail")


def ha_token():
    path = os.environ.get("HA_TOKEN_PATH")
    return Path(path).read_text().strip() if path else os.environ["SUPERVISOR_TOKEN"]


def send_mail(subject, body):
    import urllib.request

    req = urllib.request.Request(
        f"{HA_URL}/api/services/{MAIL_SERVICE}",
        data=json.dumps({"title": subject, "message": body}).encode(),
        headers={"Authorization": f"Bearer {ha_token()}", "Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=60)
ROOM_PREFIX = "climate.orchideenweg_hcu_"
BELOW_K = 0.5  # erst ab 0,5 K unter Soll zählt ein Raum als zu kalt

if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
    DAY = sys.argv[1]
else:
    DAY = (date.today() - timedelta(days=1)).isoformat()
print(f"Report für: {DAY}")
report_date = date.fromisoformat(DAY)


def source_files(base, day_str):
    """Aktuelle Datei plus die Wochenrotation des Tages (montags rotiert)."""
    y, w, _ = date.fromisoformat(day_str).isocalendar()
    files = [base.with_name(f"{base.name}.{y}-W{w:02d}.gz"), base]
    return [f for f in files if f.exists()]


_cache = {}


def read_lines(base, day_str):
    # Jede Datei nur einmal lesen, der Share ist langsam und der 7-Tage-Schnitt
    # fragt dieselben Dateien achtmal ab.
    for f in source_files(base, day_str):
        if f not in _cache:
            opener = gzip.open if f.suffix == ".gz" else open
            with opener(f, "rt") as fh:
                _cache[f] = fh.readlines()
        yield from _cache[f]


def segments(series, wanted):
    """Zeitabschnitte, in denen der Wert in `wanted` liegt."""
    out, cur_s, cur_v = [], None, None
    for ts, v in sorted(series):
        if v in wanted:
            if cur_s is None:
                cur_s, cur_v = ts, v
            elif v != cur_v:
                out.append((cur_s, ts, cur_v))
                cur_s, cur_v = ts, v
        elif cur_s is not None:
            out.append((cur_s, ts, cur_v))
            cur_s = None
    return out


def day_delta(series):
    """Tageswert eines Wochenzählers; fällt er, war der Montags-Reset dazwischen."""
    vals = [v for _, v in sorted(series)]
    if not vals:
        return None
    first, last = vals[0], vals[-1]
    return last if last < first else last - first


def collect_day(day_str):
    buf, dhw_t, outside, vl_soll, target, comp_p, starts = [], [], [], [], [], [], []
    sg, err6c = [], set()
    week = {k: [] for k in ("p_dhw", "p_heat", "q_dhw", "q_heat")}
    week_feat = {
        "heating.compressors.0.power.consumption.dhw.week": "p_dhw",
        "heating.compressors.0.power.consumption.heating.week": "p_heat",
        "heating.compressors.0.heat.production.dhw.week": "q_dhw",
        "heating.compressors.0.heat.production.heating.week": "q_heat",
    }
    n_records = 0
    mount_present = WP_JSONL.exists()
    for line in read_lines(WP_JSONL, day_str):
        if day_str not in line[:30]:
            continue
        try:
            r = json.loads(line)
            ts = datetime.fromisoformat(r["ts"]).replace(tzinfo=None)
        except (ValueError, KeyError):
            continue
        n_records += 1
        feat, v = r.get("feature", ""), r.get("values", {})
        val = v.get("value")
        if feat == "heating.bufferCylinder.sensors.temperature.main" and val is not None:
            buf.append((ts, val))
        elif feat == "heating.dhw.sensors.temperature.dhwCylinder" and val is not None:
            dhw_t.append((ts, val))
        elif feat == "heating.sensors.temperature.outside" and val is not None:
            outside.append((ts, val))
        elif feat == "heating.circuits.1.temperature" and val:  # 0 = Heizen aus
            vl_soll.append((ts, val))
        elif feat == "heating.compressors.0.heatTarget" and val is not None:
            target.append((ts, val))
        elif feat == "heating.compressors.0.power.consumption.current" and val is not None:
            comp_p.append((ts, val))
        elif feat == "heating.compressors.0.statistics" and v.get("starts") is not None:
            starts.append((ts, v["starts"]))
        elif feat == "heating.device.smartGrid" and "actualState" in v:
            sg.append((ts, v["actualState"]))
        elif feat in week_feat and val is not None:
            week[week_feat[feat]].append((ts, val))
        elif feat == "heating.coolingCircuits.0.messages":
            for e in v.get("entries", []):
                if e.get("errorCode") != "6c":
                    continue
                try:
                    dt = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
                    dt = dt.astimezone().replace(tzinfo=None)
                except (ValueError, KeyError):
                    continue
                if dt.date() == date.fromisoformat(day_str):
                    err6c.add(dt)

    comp_seq = sorted(comp_p)
    comp_ts = [t for t, _ in comp_seq]
    comp_kw = [v for _, v in comp_seq]

    def comp_running(t):
        i = bisect.bisect_right(comp_ts, t) - 1
        return i >= 0 and comp_kw[i] > 0.1

    # Nur Abschnitte mit laufendem Verdichter; eine Anforderung ohne Start (etwa
    # der frühere 60-s-Boost-Abbruch) ist kein Lauf.
    runs = [
        {"start": s, "end": e, "type": t, "dur": (e - s).total_seconds() / 60}
        for s, e, t in segments(target, {"dhw", "heating"})
        if comp_running(s) or any(s <= ct <= e and kw > 0.1 for ct, kw in comp_seq)
    ]
    # Unter 4 min oder mit 6c gilt ein Lauf als Fehlstart.
    fails = [
        x for x in runs
        if x["dur"] < 4 or any(x["start"] <= d <= x["end"] + timedelta(minutes=5) for d in err6c)
    ]

    sg_boost_min = sum(
        (e - s).total_seconds() / 60 for s, e, _ in segments(sg, {"recommendedOperation"})
    )

    # Brunnenpumpe aus der Shelly-CSV, Leerlauf heißt Pumpe an, Verdichter aus.
    pump_runs, on_s, idle_s, last_t, run_start = [], 0, 0, None, None
    shelly_present = SHELLY_CSV.exists()
    for row in csv.DictReader(read_lines(SHELLY_CSV, day_str)) if shelly_present else []:
        ts = row.get("ts", "")
        if not ts.startswith(day_str):
            continue
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
            p = float(row.get("total_w", "0") or "0")
        except ValueError:
            continue
        last_t = t
        on = p > 50
        if on:
            on_s += 1
            if not comp_running(t):
                idle_s += 1
            if run_start is None:
                run_start = t
        elif run_start is not None:
            pump_runs.append((run_start, t))
            run_start = None
    if run_start is not None and last_t:
        pump_runs.append((run_start, last_t))
    pump_idle_min = idle_s / 60 if comp_seq else None

    # WW-Ladungen nach mindestens 8 min Leerlauf hat die Automation ausgelöst.
    for x in runs:
        x["after_idle"] = x["type"] == "dhw" and any(
            s <= x["start"] - timedelta(minutes=8) and x["start"] <= e for s, e in pump_runs
        )

    def rng(series):
        vals = [v for _, v in series]
        return (min(vals), max(vals)) if vals else (None, None)

    s = sorted(starts)
    return {
        "day": day_str,
        "n_records": n_records,
        "mount_present": mount_present,
        "shelly_present": shelly_present,
        "runs": runs,
        "dhw_runs": [x for x in runs if x["type"] == "dhw"],
        "heat_runs": [x for x in runs if x["type"] == "heating"],
        "fails": fails,
        "err6c": sorted(err6c),
        "starts": (s[-1][1] - s[0][1]) if s else None,
        "p_dhw": day_delta(week["p_dhw"]),
        "p_heat": day_delta(week["p_heat"]),
        "q_dhw": day_delta(week["q_dhw"]),
        "q_heat": day_delta(week["q_heat"]),
        "dhw_t": rng(dhw_t),
        "buf": rng(buf),
        "out": rng(outside),
        "vl_soll": rng(vl_soll),
        "sg_boost_min": sg_boost_min,
        "pump_total_min": on_s / 60,
        "pump_idle_min": pump_idle_min,
    }


def rooms_below(day_str):
    """Stunden je HmIP-Raum, in denen er mehr als BELOW_K unter seinem Soll lag.

    Fenster offen senkt das Soll auf 12 °C, zählt also nicht als zu kalt.
    """
    import urllib.parse
    import urllib.request

    start = datetime.fromisoformat(day_str).astimezone()
    end = start + timedelta(days=1)
    headers = {"Authorization": f"Bearer {ha_token()}"}
    states = json.load(urllib.request.urlopen(urllib.request.Request(f"{HA_URL}/api/states", headers=headers), timeout=30))
    rooms = [x["entity_id"] for x in states if x["entity_id"].startswith(ROOM_PREFIX)]
    url = (
        f"{HA_URL}/api/history/period/{urllib.parse.quote(start.isoformat())}"
        f"?end_time={urllib.parse.quote(end.isoformat())}&filter_entity_id={','.join(rooms)}"
    )
    hist = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60))
    out = []
    for series in hist:
        name = series[0]["attributes"].get("friendly_name", series[0]["entity_id"]).split(" Orchideenweg")[0].strip()
        below_s, worst = 0.0, 0.0
        pts = [(datetime.fromisoformat(x["last_changed"]), x["attributes"]) for x in series]
        for (t0, a), (t1, _) in zip(pts, pts[1:] + [(min(end, datetime.now().astimezone()), None)]):
            cur, sp = a.get("current_temperature"), a.get("temperature")
            if cur is None or sp is None:
                continue
            diff = cur - sp
            if diff < -BELOW_K:
                below_s += (t1 - max(t0, start)).total_seconds()
            worst = min(worst, diff)
        if below_s >= 1800:
            out.append((name, below_s / 3600, worst))
    return sorted(out, key=lambda x: -x[1])


def fmt(v, suffix="", dec=1):
    return "-" if v is None else f"{v:.{dec}f}{suffix}"


def rng_fmt(r, suffix="°C"):
    lo, hi = r
    return "-" if lo is None else f"{lo:.1f} bis {hi:.1f}{suffix}"


def cop(p, q):
    return f"{q / p:.1f}" if p and q else "-"


def run_lines(runs):
    return "".join(
        f"  {x['start']:%H:%M}-{x['end']:%H:%M}  {x['dur']:.0f} min"
        f"{'  nach Leerlauf' if x.get('after_idle') else ''}\n"
        for x in runs
    )


r = collect_day(DAY)

# Ohne Daten sähe der Tag aus wie ein perfekter Tag ohne Läufe.
if not r["mount_present"]:
    data_problem = f"{SHARE} nicht verfügbar"
elif r["n_records"] == 0:
    data_problem = f"0 WP-Records für {DAY} gelesen (Upstream-Logger oder Mount?)"
else:
    data_problem = None

if data_problem:
    subject = f"WP {DAY}: ⚠️ KEINE DATEN, {data_problem}"
    body = (
        f"Tagesreport Viessmann CU401B, {DAY}\n\n"
        f"{data_problem}\n\n"
        f"Gelesene WP-Records: {r['n_records']}\n"
        f"Mount vorhanden: {r['mount_present']}, Shelly-CSV vorhanden: {r['shelly_present']}\n\n"
        f"Es wurde nichts gemessen, das ist nicht dasselbe wie ein ruhiger Tag.\n"
    )
    print(f"\n=== Mail-Subject ===\n{subject}\n\n=== Mail-Body ===\n{body}")
    if "--dry-run" not in sys.argv:
        send_mail(subject=subject, body=body)
    sys.exit(1)

prev = [
    p for p in (collect_day((report_date - timedelta(days=i)).isoformat()) for i in range(1, 8))
    if p["n_records"] > 0
]


def avg(key, f=lambda v: v):
    vals = [f(p[key]) for p in prev if p[key] is not None]
    return sum(vals) / len(vals) if vals else None


dhw, heat = r["dhw_runs"], r["heat_runs"]
dhw_min = sum(x["dur"] for x in dhw)
heat_min = sum(x["dur"] for x in heat)
idle_kwh = r["pump_idle_min"] * PUMP_W / 60000 if r["pump_idle_min"] is not None else None

body = f"""Tagesreport Viessmann CU401B, {DAY}
{'=' * 50}

WARMWASSER
  Ladungen:         {len(dhw)} ({dhw_min:.0f} min), davon nach Leerlauf {sum(x['after_idle'] for x in dhw)}
  Strom / Wärme:    {fmt(r['p_dhw'], ' kWh')} / {fmt(r['q_dhw'], ' kWh')}, COP {cop(r['p_dhw'], r['q_dhw'])}
  Speicher:         {rng_fmt(r['dhw_t'])}
{run_lines(dhw)}
HEIZEN
  Läufe:            {len(heat)} ({heat_min:.0f} min)
  Strom / Wärme:    {fmt(r['p_heat'], ' kWh')} / {fmt(r['q_heat'], ' kWh')}, COP {cop(r['p_heat'], r['q_heat'])}
  Puffer:           {rng_fmt(r['buf'])}
  Vorlauf-Soll:     {rng_fmt(r['vl_soll'])}
  Außen:            {rng_fmt(r['out'])}
{run_lines(heat)}
BRUNNENPUMPE
  Laufzeit:         {fmt(r['pump_total_min'], ' min', 0)}
  Leerlauf:         {fmt(r['pump_idle_min'], ' min', 0)} (~{fmt(idle_kwh, ' kWh')})

VERDICHTER
  Starts:           {fmt(r['starts'], '', 0)}
  Fehlstarts:       {len(r['fails'])} (unter 4 min oder mit 6c)
  6c-Meldungen:     {len(r['err6c'])}{''.join(f"  {d:%H:%M}" for d in r['err6c'])}
"""
try:
    cold = rooms_below(DAY)
    body += f"\nRÄUME UNTER SOLL (mehr als {BELOW_K} K, ab 30 min)\n"
    body += "".join(f"  {n:22} {h:4.1f} h, max {w:+.1f} K\n" for n, h, w in cold) or "  keine\n"
except Exception as err:  # HA nicht erreichbar soll den Report nicht verhindern
    cold = None
    body += f"\nRÄUME UNTER SOLL: keine Daten ({err})\n"
if r["sg_boost_min"] > 0:
    body += f"  SG-Boost:         {r['sg_boost_min']:.0f} min\n"
if not r["shelly_present"]:
    body += "\nHinweis: Brunnen-CSV fehlte, Pumpenwerte unvollständig.\n"
if prev:
    body += f"""
SCHNITT DER {len(prev)} TAGE DAVOR
  WW Strom:         {fmt(avg('p_dhw'), ' kWh/Tag')}
  Heizen Strom:     {fmt(avg('p_heat'), ' kWh/Tag')}
  Leerlauf:         {fmt(avg('pump_idle_min'), ' min/Tag', 0)}
  Starts:           {fmt(avg('starts'), '/Tag')}
"""

subject = (
    f"WP {DAY}: WW {len(dhw)}× {fmt(r['p_dhw'], ' kWh')} | Heizen {len(heat)}× {fmt(r['p_heat'], ' kWh')}"
    f" | Leerlauf {fmt(r['pump_idle_min'], ' min', 0)} | Außen {fmt(r['out'][0], '', 0)}-{fmt(r['out'][1], '°C', 0)}"
)
if cold:
    subject += f" | 🥶 {len(cold)} Räume unter Soll"
if r["err6c"]:
    subject += f" | ⚠ {len(r['err6c'])}×6c"
if r["fails"]:
    subject += f" | ⚠ {len(r['fails'])} Fehlstarts"

print(f"\n=== Mail-Subject ===\n{subject}\n\n=== Mail-Body ===\n{body}")
if "--dry-run" not in sys.argv:
    send_mail(subject=subject, body=body)
    print(f"\nMail verschickt über {MAIL_SERVICE}")
else:
    print("\n[Dry-Run] Mail nicht verschickt.")

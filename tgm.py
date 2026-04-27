#!/usr/bin/env python3
"""
TGM — Tank Gauge Monitor
=========================
Reads from Veeder-Root TLS gauges over serial or network,
bridges data to a central hub, and serves on-demand readings.

All settings live in /etc/tgm/site.json
Edit with:  sudo tgm settings edit
"""

import argparse
import json
import logging
import os
import re
import select
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

import serial

# ---------------------------------------------------------------------------
# Fixed paths — same on every unit
# ---------------------------------------------------------------------------
SITE_FILE   = "/etc/tgm/site.json"
JOURNAL_DIR = "/var/log/tgm"
CACHE_DIR   = "/var/lib/tgm"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
lg = logging.getLogger("tgm")


# ===================================================================
# Site file
# ===================================================================
def read_site() -> dict:
    with open(SITE_FILE) as fh:
        return json.load(fh)


# ===================================================================
# Identity
# ===================================================================
def hw_addr() -> str:
    raw = uuid.getnode()
    return "".join(f"{(raw >> (8 * (5 - i))) & 0xFF:02x}" for i in range(6))


def unit_label(site: dict) -> str:
    tag = site.get("site", {}).get("name", "")
    if tag and tag != "mrb_XXXXXXXXXXXX":
        return tag
    return f"mrb_{hw_addr()}"


# ===================================================================
# Gauge link — serial or network
# ===================================================================
_PARITY = {"odd": "O", "even": "E", "none": "N"}


def dial_gauge_serial(site: dict):
    s = site.get("gauge", {}).get("serial", {})
    dev  = s.get("device", "/dev/ttyUSB0")
    baud = s.get("baudrate", 9600)
    par  = _PARITY.get(s.get("parity", "odd"), "O")
    db   = s.get("databits", 7)
    sb   = s.get("stopbits", 1)
    lg.info("Gauge serial %s %d/%s", dev, baud, s.get("parity", "odd"))
    return serial.Serial(
        port=dev, baudrate=baud, parity=par,
        bytesize=db, stopbits=sb, timeout=0,
    )


def dial_gauge_net(site: dict) -> socket.socket:
    n = site.get("gauge", {}).get("network", {})
    addr = n.get("address", "192.168.1.198")
    pt   = n.get("port", 10001)
    lg.info("Gauge network %s:%d", addr, pt)
    sk = socket.create_connection((addr, pt), timeout=10)
    sk.setblocking(False)
    return sk


def dial_gauge(site: dict):
    if site.get("gauge", {}).get("mode", "serial") == "network":
        return dial_gauge_net(site)
    return dial_gauge_serial(site)


def hangup(conn):
    try:
        conn.close()
    except OSError:
        pass


# ===================================================================
# Hub link — raw TCP to central server
# ===================================================================
def dial_hub(site: dict) -> socket.socket:
    h = site.get("hub", {})
    addr = h.get("address", "")
    pt   = h.get("port", 5000)
    if not addr:
        raise ValueError("hub.address missing in site.json")
    lg.info("Hub %s:%d", addr, pt)
    sk = socket.create_connection((addr, pt), timeout=10)
    mac = hw_addr()
    sk.sendall(f"{mac}\n".encode())
    lg.info("Registered %s", mac)
    sk.setblocking(False)
    return sk


# ===================================================================
# Bridge — bidirectional pipe between hub and gauge
# ===================================================================
def bridge(hub_sk, gauge_conn, wait_sec: int):
    hfd = hub_sk.fileno()
    gfd = gauge_conn.fileno()
    watch = [hfd, gfd]
    lg.info("Bridge up (idle limit %ds)", wait_sec)

    while True:
        ready, _, bad = select.select(watch, [], watch, wait_sec)

        if not ready and not bad:
            lg.info("Bridge idle, closing")
            return
        if bad:
            lg.warning("Bridge error")
            return

        for fd in ready:
            if fd == hfd:
                chunk = hub_sk.recv(4096)
                if not chunk:
                    lg.info("Hub disconnected")
                    return
                lg.debug("Hub>Gauge %d b", len(chunk))
                if isinstance(gauge_conn, serial.Serial):
                    gauge_conn.write(chunk)
                else:
                    gauge_conn.sendall(chunk)

            elif fd == gfd:
                if isinstance(gauge_conn, serial.Serial):
                    chunk = gauge_conn.read(4096)
                else:
                    chunk = gauge_conn.recv(4096)
                if not chunk:
                    lg.info("Gauge disconnected")
                    return
                lg.debug("Gauge>Hub %d b", len(chunk))
                hub_sk.sendall(chunk)


# ===================================================================
# TLS probe commands
# ===================================================================
_PROBES = {
    "levels":     b"\x01I20100\r",
    "deliveries": b"\x01I20200\r",
    "leak_check": b"\x01I20700\r",
    "leak_log":   b"\x01I25100\r",
}


def _talk_serial(conn, packet: bytes) -> str:
    conn.reset_input_buffer()
    conn.write(packet)
    time.sleep(1.5)
    conn.timeout = 5
    answer = conn.read(4096)
    conn.timeout = 0
    return answer.decode("ascii", errors="ignore").replace("\x00", "")


def _talk_net(site: dict, packet: bytes) -> str:
    n = site.get("gauge", {}).get("network", {})
    addr = n.get("address", "192.168.1.198")
    pt   = n.get("port", 10001)
    with socket.create_connection((addr, pt), timeout=10) as sk:
        sk.sendall(packet)
        time.sleep(1.5)
        parts = []
        sk.settimeout(3)
        try:
            while True:
                d = sk.recv(4096)
                if not d:
                    break
                parts.append(d)
        except socket.timeout:
            pass
    return b"".join(parts).decode("ascii", errors="ignore").replace("\x00", "")


def _ask(site: dict, wire, packet: bytes, tag: str) -> str:
    sched = site.get("schedule", {})
    tries = sched.get("retry_attempts", 2)
    gap   = sched.get("pause_between_cmds_sec", 3)
    is_wire = site.get("gauge", {}).get("mode", "serial") == "serial"

    for n in range(1, tries + 1):
        try:
            return _talk_serial(wire, packet) if is_wire else _talk_net(site, packet)
        except Exception as err:
            if n < tries:
                lg.warning("  %s #%d (%s), pause %ds", tag, n, err, gap)
                time.sleep(gap)
            else:
                raise


# ===================================================================
# Decoders
# ===================================================================
def decode_levels(txt: str) -> list:
    rows = []
    for ln in txt.splitlines():
        m = re.match(
            r"\s*(\d{1,2})\s+(.+?)\s{2,}"
            r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+"
            r"([\d.]+)\s+([\d.]+)\s+([\d.]+)", ln)
        if m:
            rows.append({
                "tank":       int(m.group(1)),
                "fuel":       m.group(2).strip(),
                "gallons":    float(m.group(3)),
                "tc_gallons": float(m.group(4)),
                "ullage":     float(m.group(5)),
                "inches":     float(m.group(6)),
                "water":      float(m.group(7)),
                "temp":       float(m.group(8)),
            })
    return rows if rows else [{"text": txt}]


def decode_deliveries(txt: str):
    drops = []
    cur = None
    for ln in txt.splitlines():
        ln = ln.strip()
        tm = re.match(r"T\s*(\d+)\s*:\s*(.+)", ln)
        if tm:
            cur = {"tank": int(tm.group(1)), "fuel": tm.group(2).strip()}
            continue
        em = re.match(
            r"END:\s+(.+?)\s{2,}([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)", ln)
        if em and cur:
            drops.append({
                "tank":        cur["tank"],
                "fuel":        cur["fuel"],
                "finished":    em.group(1).strip(),
                "final_gal":   float(em.group(2)),
                "final_tc":    float(em.group(3)),
                "final_water": float(em.group(4)),
                "final_temp":  float(em.group(5)),
                "final_ht":    float(em.group(6)),
            })
        am = re.match(r"AMOUNT:\s+([\d.]+)\s+([\d.]+)", ln)
        if am and drops:
            drops[-1]["drop_gal"] = float(am.group(1))
            drops[-1]["drop_tc"]  = float(am.group(2))
    return drops if drops else {"text": txt.strip()}


def decode_leaks(txt: str) -> list:
    hits = []
    for ln in txt.splitlines():
        m = re.match(
            r"\s*(\d{1,2})\s+(.+?)\s{2,}(?:PER:\s*)?(.+?)\s+(PASS|FAIL)", ln)
        if m:
            hits.append({
                "tank":   int(m.group(1)),
                "fuel":   m.group(2).strip(),
                "when":   m.group(3).strip(),
                "verdict": m.group(4),
            })
    return hits if hits else {"text": txt.strip()}


# ===================================================================
# Snapshot — run all probes, decode, return payload
# ===================================================================
def snapshot(site: dict, raw_out: bool = False) -> dict:
    label = unit_label(site)
    gmode = site.get("gauge", {}).get("mode", "serial")
    gap   = site.get("schedule", {}).get("pause_between_cmds_sec", 3)

    pkg = {
        "unit":       label,
        "hw":         hw_addr(),
        "link":       gmode,
        "taken_at":   datetime.now(timezone.utc).isoformat(),
        "readings":   {},
    }

    wire = None
    if gmode == "serial":
        wire = dial_gauge_serial(site)

    for idx, (tag, packet) in enumerate(_PROBES.items()):
        if idx > 0:
            lg.info("  pause %ds", gap)
            time.sleep(gap)

        lg.info(">> %s", tag)
        try:
            txt = _ask(site, wire, packet, tag)
            if raw_out:
                pkg["readings"][tag] = {"text": txt}
            elif tag == "levels":
                pkg["readings"][tag] = decode_levels(txt)
            elif tag == "deliveries":
                pkg["readings"][tag] = decode_deliveries(txt)
            elif tag in ("leak_check", "leak_log"):
                pkg["readings"][tag] = decode_leaks(txt)
            else:
                pkg["readings"][tag] = {"text": txt.strip()}
        except Exception as ex:
            lg.error("  %s failed: %s", tag, ex)
            pkg["readings"][tag] = {"fault": str(ex)}

    if wire:
        hangup(wire)

    os.makedirs(CACHE_DIR, exist_ok=True)
    fp = os.path.join(CACHE_DIR, "last.json")
    with open(fp, "w") as fh:
        json.dump(pkg, fh, indent=2)
    lg.info("Cached %s", fp)

    return pkg


# ===================================================================
# Action: bridge (production)
# ===================================================================
def act_bridge(site: dict):
    wait = site.get("bridge", {}).get("timeout_sec", 120)
    hub = None
    gauge = None
    try:
        hub   = dial_hub(site)
        gauge = dial_gauge(site)
        bridge(hub, gauge, wait)
    except Exception as ex:
        lg.error("Bridge fault: %s", ex)
        raise
    finally:
        if gauge: hangup(gauge)
        if hub:   hangup(hub)


# ===================================================================
# Action: check (one-shot field test)
# ===================================================================
def act_check(site: dict, raw_out: bool = False):
    result = snapshot(site, raw_out=raw_out)
    print(json.dumps(result, indent=2))


# ===================================================================
# Action: watch (scheduled loop)
# ===================================================================
def act_watch(site: dict):
    gap = site.get("schedule", {}).get("interval_sec", 3600)
    lg.info("Watching every %ds", gap)
    while True:
        try:
            snapshot(site)
        except KeyboardInterrupt:
            lg.info("Stopped")
            return
        except Exception as ex:
            lg.error("Watch fault: %s", ex)
        lg.info("Next in %ds", gap)
        time.sleep(gap)


# ===================================================================
# Action: serve (watch + on-demand HTTP)
# ===================================================================
_live_site = {}


class _Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _dispatch(self):
        ep = self.path.rstrip("/")

        if ep == "/read":
            lg.info("On-demand read from %s", self.client_address[0])
            out = snapshot(_live_site)
            self._send(200, out)

        elif ep == "/health":
            self._send(200, {
                "unit":     unit_label(_live_site),
                "hw":       hw_addr(),
                "link":     _live_site.get("gauge", {}).get("mode", "serial"),
                "interval": _live_site.get("schedule", {}).get("interval_sec", 3600),
                "up":       True,
            })

        else:
            self._send(404, {"fault": "use /read or /health"})

    def _send(self, code, obj):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def act_serve(site: dict):
    global _live_site
    _live_site = site
    pt = site.get("ondemand", {}).get("http_port", 5050)

    Thread(target=act_watch, args=(site,), daemon=True).start()

    srv = HTTPServer(("0.0.0.0", pt), _Handler)
    lg.info("Serving http://0.0.0.0:%d/read", pt)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        lg.info("Stopped")


# ===================================================================
# Action: info
# ===================================================================
def act_info(site: dict):
    _banner(site, "info")
    cached = os.path.join(CACHE_DIR, "last.json")
    if os.path.exists(cached):
        ts = os.path.getmtime(cached)
        print(f"  LAST READ:    {datetime.fromtimestamp(ts):%Y-%m-%d %H:%M:%S}")
        with open(cached) as fh:
            data = json.load(fh)
        for row in data.get("readings", {}).get("levels", []):
            if "tank" in row:
                print(f"  Tank {row['tank']}:       {row.get('fuel','?')} — {row.get('gallons','?')} gal")
    else:
        print("  LAST READ:    none yet")
    print("=" * 60)


# ===================================================================
# Action: settings
# ===================================================================
def act_settings(op: str):
    if op == "edit":
        os.execvp("nano", ["nano", SITE_FILE])
    else:
        try:
            s = read_site()
            clean = {k: v for k, v in s.items() if k != "help"}
            print(json.dumps(clean, indent=2))
        except FileNotFoundError:
            print(f"Not found: {SITE_FILE}")
            print("Run: sudo ./setup.sh")


# ===================================================================
# Action: journal
# ===================================================================
def act_journal(faults_only: bool):
    lf = os.path.join(JOURNAL_DIR, "tgm.log")
    if not os.path.exists(lf):
        print(f"No entries. File: {lf}")
        return
    if faults_only:
        os.system(f"grep -i 'error\\|fail\\|fault' {lf} | tail -50")
    else:
        os.system(f"tail -100 {lf}")


# ===================================================================
# Banner
# ===================================================================
def _banner(site: dict, mode: str):
    g = site.get("gauge", {})
    h = site.get("hub", {})
    gm = g.get("mode", "serial")
    print("=" * 60)
    print("  UNIT:        ", unit_label(site))
    print("  HW ADDR:     ", hw_addr())
    print("  ACTION:      ", mode.upper())
    if gm == "serial":
        s = g.get("serial", {})
        print(f"  GAUGE:        SERIAL  ({s.get('device')}, {s.get('baudrate')} baud, {s.get('parity')} parity)")
    else:
        n = g.get("network", {})
        print(f"  GAUGE:        NETWORK ({n.get('address')}:{n.get('port')})")
    if mode in ("bridge", "run"):
        print("  HUB:         ", f"{h.get('address')}:{h.get('port')}")
        print("  IDLE LIMIT:  ", f"{site.get('bridge', {}).get('timeout_sec', 120)}s")
    if mode in ("watch", "serve"):
        print("  INTERVAL:    ", f"{site.get('schedule', {}).get('interval_sec', 3600)}s")
    if mode == "serve":
        print("  HTTP:        ", f"http://0.0.0.0:{site.get('ondemand', {}).get('http_port', 5050)}/read")
    print("=" * 60)


# ===================================================================
# CLI
# ===================================================================
def main():
    ap = argparse.ArgumentParser(description="TGM — Tank Gauge Monitor")
    sp = ap.add_subparsers(dest="action")

    sp.add_parser("bridge",  help="Production: pipe data between hub and gauge")
    sp.add_parser("run",     help="Alias for bridge")

    ck = sp.add_parser("check", help="One-shot field test")
    ck.add_argument("--raw", action="store_true", help="Raw gauge output")

    sp.add_parser("watch",  help="Scheduled readings")
    sp.add_parser("serve",  help="Scheduled + on-demand HTTP")
    sp.add_parser("info",   help="Unit info and last reading")

    st = sp.add_parser("settings", help="Show or edit site config")
    st.add_argument("op", nargs="?", default="show", choices=["show", "edit"])

    jn = sp.add_parser("journal", help="View log entries")
    jn.add_argument("--faults", action="store_true")

    sp.add_parser("selftest", help="Run integration tests")

    args = ap.parse_args()
    act  = args.action or "bridge"

    if act == "settings":
        act_settings(args.op)
        return
    if act == "journal":
        act_journal(args.faults)
        return
    if act == "selftest":
        tp = "/opt/tgm/integration_test.py"
        if os.path.exists(tp):
            os.execvp("python3", ["python3", tp])
        else:
            print("Not found:", tp)
        return

    try:
        site = read_site()
    except FileNotFoundError:
        print(f"No site config: {SITE_FILE}")
        print("Run: sudo ./setup.sh")
        sys.exit(1)

    if act == "info":
        act_info(site)
    elif act in ("bridge", "run"):
        _banner(site, "bridge")
        try:
            act_bridge(site)
        except Exception:
            sys.exit(1)
    elif act == "check":
        _banner(site, "check")
        act_check(site, raw_out=args.raw)
    elif act == "watch":
        _banner(site, "watch")
        act_watch(site)
    elif act == "serve":
        _banner(site, "serve")
        act_serve(site)


if __name__ == "__main__":
    main()

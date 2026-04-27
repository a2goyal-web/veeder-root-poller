#!/usr/bin/env python3
"""
Veeder-Root Tank Gauge Poller for Raspberry Pi
===============================================
Polls a Veeder-Root TLS-350/450 gauge via Serial or TCP,
parses tank data, and POSTs results to your server.

Install on Pi:
  pip3 install pyserial requests --break-system-packages

Run:
  python3 veeder_root_poller.py              # starts scheduled polling
  python3 veeder_root_poller.py --pull       # one-time on-demand pull (for field testing)
  python3 veeder_root_poller.py --listen     # start HTTP listener for on-demand button

PM2:
  pm2 start veeder_root_poller.py --interpreter python3 --name vr-poller
"""

# ==========================================================================
# ██████  CONFIGURATION — EDIT THIS SECTION ONLY  ██████
# ==========================================================================
#
#  Every field a technician needs to change is right here.
#  Nothing below this block needs to be touched.
#
# --------------------------------------------------------------------------

# 1. DEVICE NAME
#    The unique name for THIS Pi / tank gauge.
#    Format: mrb_<mac_address>  (run `ip link show` to find the MAC)
DEVICE_NAME = "mrb_XXXXXXXXXXXX"

# 2. CONNECTION TYPE
#    "serial"  →  Pi is wired directly to the gauge via USB-to-serial
#    "tcp"     →  Pi reaches the gauge over the network (Lantronix/Moxa)
CONNECTION_TYPE = "serial"

# 3. SERIAL SETTINGS  (used when CONNECTION_TYPE = "serial")
SERIAL_PORT     = "/dev/ttyUSB0"
SERIAL_BAUD     = 9600
SERIAL_PARITY   = "odd"       # "odd" | "even" | "none"
SERIAL_DATABITS = 7
SERIAL_STOPBITS = 1

# 4. TCP SETTINGS  (used when CONNECTION_TYPE = "tcp")
TCP_IP   = "192.168.1.198"
TCP_PORT = 10001

# 5. SERVER — where tank data gets POSTed
SERVER_IP   = "18.189.243.23"
SERVER_PORT = 8080
SERVER_PATH = "/api/tank-data"      # URL path on the server

# 6. POLL SCHEDULE
#    How often (in seconds) to poll the gauge automatically.
#      60    = every minute
#      300   = every 5 minutes
#      3600  = every hour
POLL_INTERVAL_SECONDS = 60

# 7. ON-DEMAND LISTENER PORT
#    When running with --listen, the Pi opens this port so your
#    frontend can hit  http://<pi-ip>:5050/pull  to trigger a read.
LISTEN_PORT = 5050

# ==========================================================================
# ██████  END OF CONFIGURATION — DO NOT EDIT BELOW THIS LINE  ██████
# ==========================================================================


import argparse
import json
import logging
import re
import socket
import sys
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

import serial
import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("vr-poller")

# ---------------------------------------------------------------------------
# Derived values (built from config above — don't edit)
# ---------------------------------------------------------------------------
SERVER_URL = f"http://{SERVER_IP}:{SERVER_PORT}{SERVER_PATH}"

PARITY_MAP = {
    "odd":  serial.PARITY_ODD,
    "even": serial.PARITY_EVEN,
    "none": serial.PARITY_NONE,
}

# Veeder-Root TLS protocol commands
VR_COMMANDS = {
    "inventory":      b"\x01I20100\r",   # current tank levels
    "last_delivery":  b"\x01I20200\r",   # last delivery record
    "leak_test":      b"\x01I20700\r",   # last leak-test result
    "leak_history":   b"\x01I25100\r",   # 12-month leak-test history
}


# ===================================================================
# Transport layer
# ===================================================================
class SerialTransport:
    """Talk to the gauge over /dev/ttyUSB0."""

    def __init__(self):
        self.cfg = dict(
            port=SERIAL_PORT,
            baudrate=SERIAL_BAUD,
            parity=PARITY_MAP.get(SERIAL_PARITY, serial.PARITY_ODD),
            bytesize=SERIAL_DATABITS,
            stopbits=SERIAL_STOPBITS,
            timeout=5,
        )

    def send(self, cmd: bytes) -> str:
        with serial.Serial(**self.cfg) as ser:
            ser.reset_input_buffer()
            ser.write(cmd)
            time.sleep(1.5)
            raw = ser.read(4096)
        return self._clean(raw)

    @staticmethod
    def _clean(raw: bytes) -> str:
        return raw.decode("ascii", errors="ignore").replace("\x00", "")


class TcpTransport:
    """Talk to the gauge over TCP (serial-server converter)."""

    def __init__(self):
        self.host = TCP_IP
        self.port = TCP_PORT

    def send(self, cmd: bytes) -> str:
        with socket.create_connection((self.host, self.port), timeout=10) as sock:
            sock.sendall(cmd)
            time.sleep(1.5)
            chunks = []
            sock.settimeout(3)
            try:
                while True:
                    data = sock.recv(4096)
                    if not data:
                        break
                    chunks.append(data)
            except socket.timeout:
                pass
        return self._clean(b"".join(chunks))

    @staticmethod
    def _clean(raw: bytes) -> str:
        return raw.decode("ascii", errors="ignore").replace("\x00", "")


def get_transport():
    """Return the right transport based on CONNECTION_TYPE config."""
    if CONNECTION_TYPE == "tcp":
        log.info("Transport: TCP -> %s:%s", TCP_IP, TCP_PORT)
        return TcpTransport()
    else:
        log.info("Transport: Serial -> %s @ %s baud", SERIAL_PORT, SERIAL_BAUD)
        return SerialTransport()


# ===================================================================
# Veeder-Root response parser
# ===================================================================
def parse_inventory(raw: str) -> list:
    """
    Parse i20100 (inventory) into structured tank records.
    Typical line:  01  UNLEADED   8045.00  52.31  0.00  67.40
    """
    tanks = []
    for line in raw.splitlines():
        line = line.strip()
        m = re.match(
            r"(\d{2})\s+"           # tank number
            r"(.+?)\s{2,}"          # product label
            r"([\d.]+)\s+"          # volume (gallons)
            r"([\d.]+)\s+"          # height (inches)
            r"([\d.]+)\s+"          # water level (inches)
            r"([\d.]+)",            # temperature (F)
            line,
        )
        if m:
            tanks.append({
                "tank":       int(m.group(1)),
                "product":    m.group(2).strip(),
                "volume_gal": float(m.group(3)),
                "height_in":  float(m.group(4)),
                "water_in":   float(m.group(5)),
                "temp_f":     float(m.group(6)),
            })
    if not tanks:
        tanks.append({"raw": raw})
    return tanks


def parse_raw(raw: str) -> dict:
    """Store as raw text for commands we don't parse yet."""
    return {"raw": raw.strip()}


# ===================================================================
# Core: pull data from gauge + POST to server
# ===================================================================
def pull_data(transport) -> dict:
    """
    Run every VR command, build the payload, POST it, return it.
    This is the ONE function called by the scheduler, the CLI --pull
    flag, AND the on-demand HTTP endpoint. Same command, same device,
    same data — ready for a frontend button later.
    """
    payload = {
        "device_id":   DEVICE_NAME,
        "connection":  CONNECTION_TYPE,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "data":        {},
    }

    for label, cmd in VR_COMMANDS.items():
        log.info("-> %s", label)
        try:
            raw = transport.send(cmd)
            log.debug("  raw[:%d]: %s", min(len(raw), 120), repr(raw[:120]))
            if label == "inventory":
                payload["data"][label] = parse_inventory(raw)
            else:
                payload["data"][label] = parse_raw(raw)
        except Exception as exc:
            log.error("  FAIL %s: %s", label, exc)
            payload["data"][label] = {"error": str(exc)}

    # POST to server
    try:
        log.info("POST -> %s", SERVER_URL)
        resp = requests.post(SERVER_URL, json=payload, timeout=15)
        log.info("  Server: %s %s", resp.status_code, resp.reason)
        payload["server_response"] = resp.status_code
    except Exception as exc:
        log.error("  POST failed: %s", exc)
        payload["server_response"] = str(exc)

    # Local copy for field debugging
    with open("/tmp/vr_latest.json", "w") as f:
        json.dump(payload, f, indent=2)
    log.info("Saved /tmp/vr_latest.json")

    return payload


# ===================================================================
# Scheduled polling loop
# ===================================================================
def run_scheduler(transport):
    """Poll forever at POLL_INTERVAL_SECONDS."""
    log.info(
        "Starting scheduler: every %ds | device=%s | connection=%s",
        POLL_INTERVAL_SECONDS, DEVICE_NAME, CONNECTION_TYPE,
    )
    while True:
        try:
            pull_data(transport)
        except KeyboardInterrupt:
            log.info("Stopped by user")
            sys.exit(0)
        except Exception as exc:
            log.error("Poll cycle error: %s", exc)
        log.info("Next poll in %ds ...", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


# ===================================================================
# On-demand HTTP listener  (for the future frontend button)
# ===================================================================
# The Pi listens on LISTEN_PORT.  Any GET or POST to /pull triggers
# a gauge read and returns the JSON payload.  The device name is
# baked in, so the frontend only talks to the Pi it's mapped to.
#
#   curl http://<pi-ip>:5050/pull
#   or wire a button:  fetch("http://<pi-ip>:5050/pull")
# ===================================================================

_transport_ref = None  # set at startup so the handler can reach it


class OnDemandHandler(BaseHTTPRequestHandler):
    """Tiny HTTP handler — only responds to /pull and /status."""

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        path = self.path.rstrip("/")

        # /status — quick health check, no gauge read
        if path == "/status":
            info = {
                "device_id":   DEVICE_NAME,
                "connection":  CONNECTION_TYPE,
                "poll_interval": POLL_INTERVAL_SECONDS,
                "status":      "running",
            }
            self._respond(200, info)
            return

        # /pull — triggers a full gauge read + server POST
        if path == "/pull":
            log.info("On-demand pull triggered from %s", self.client_address[0])
            result = pull_data(_transport_ref)
            self._respond(200, result)
            return

        self._respond(404, {"error": "use /pull or /status"})

    def _respond(self, code, body_dict):
        body = json.dumps(body_dict, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # silence default HTTP log noise


def run_listener(transport):
    """
    Start HTTP listener + scheduler side by side.
    The listener lets the frontend (or curl) trigger an instant read.
    The scheduler keeps the regular polling going in the background.
    """
    global _transport_ref
    _transport_ref = transport

    # Scheduler in background thread
    t = Thread(target=run_scheduler, args=(transport,), daemon=True)
    t.start()

    server = HTTPServer(("0.0.0.0", LISTEN_PORT), OnDemandHandler)
    log.info("On-demand listener ready -> http://0.0.0.0:%d/pull", LISTEN_PORT)
    log.info("Frontend button URL -> http://<this-pi-ip>:%d/pull", LISTEN_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Listener stopped")


# ===================================================================
# CLI entry point
# ===================================================================
def main():
    p = argparse.ArgumentParser(
        description="Veeder-Root tank gauge poller",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 veeder_root_poller.py              # scheduled polling (default)
  python3 veeder_root_poller.py --pull       # one-shot field test
  python3 veeder_root_poller.py --listen     # scheduler + on-demand HTTP endpoint
        """,
    )
    p.add_argument(
        "--pull", action="store_true",
        help="Pull data once and exit (field testing)",
    )
    p.add_argument(
        "--listen", action="store_true",
        help="Start HTTP listener on port %d for on-demand pulls + scheduled polling" % LISTEN_PORT,
    )
    args = p.parse_args()

    # Print active config so the tech can verify at a glance
    print("=" * 60)
    print("  DEVICE:      ", DEVICE_NAME)
    print("  CONNECTION:  ", CONNECTION_TYPE.upper())
    if CONNECTION_TYPE == "serial":
        print("  SERIAL PORT: ", SERIAL_PORT)
        print("  BAUD / PAR:  ", SERIAL_BAUD, "/", SERIAL_PARITY)
    else:
        print("  TCP TARGET:  ", f"{TCP_IP}:{TCP_PORT}")
    print("  SERVER:      ", SERVER_URL)
    print("  POLL EVERY:  ", f"{POLL_INTERVAL_SECONDS}s")
    if args.listen:
        print("  ON-DEMAND:   ", f"http://0.0.0.0:{LISTEN_PORT}/pull")
    print("=" * 60)

    transport = get_transport()

    if args.pull:
        result = pull_data(transport)
        print(json.dumps(result, indent=2))
    elif args.listen:
        run_listener(transport)
    else:
        run_scheduler(transport)


if __name__ == "__main__":
    main()

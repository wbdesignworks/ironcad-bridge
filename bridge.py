#!/usr/bin/env python3
"""
IronCAD SDR bridge.

Runs on the machine beside your radio receiver. Polls the local SDR appliance
(github.com/hydro13/sdr, MIT) and pushes what it heard to IronCAD over HTTPS.

WHY THIS EXISTS AT ALL
    The appliance sits inside your network, next to an antenna. IronCAD runs in
    the cloud and cannot route to a host on your LAN, and port-forwarding a radio
    receiver to the public internet would be a worse idea than having no feature.
    So nothing reaches in: this process reaches OUT, on an interval, over an
    ordinary HTTPS connection. No inbound firewall rule, no tunnel, no static IP.

RECEIVE ONLY
    This bridge reads three receive decoders and nothing else. It does not touch
    the appliance's transmit apps (aprs-tx, pocsag-tx, siggen) and will refuse to
    start if asked to. Transmitting on public-safety or amateur frequencies is
    governed by the licence for those frequencies and by the type-acceptance of
    the hardware; a HackRF driven by dispatch software satisfies neither. The
    IronCAD server independently refuses any push carrying transmit intent, so
    this is belt and braces on purpose.

WHAT IT SENDS
    aprs_packets  raw TNC2 text, decoded server-side so RF positions land in
                  tak_tracks beside every other responder position. The
                  appliance's own APRS parser is NOT used: it reads uncompressed
                  reports only, while IronCAD also decodes base91-compressed.
    aircraft      locally received ADS-B
    weather       433 MHz weather-sensor telemetry

USAGE
    pip install requests
    export IRONCAD_AGENCY_ID=...        # shown in IronCAD settings
    export IRONCAD_INGEST_KEY=...       # issued once in IronCAD settings
    python bridge.py

    Optional:
      SDR_BASE_URL     default http://127.0.0.1:5051
      IRONCAD_API      default https://api.ironcad.tech
      SITE_LABEL       e.g. "Station 3 roof"
      POLL_SECONDS     default 10
"""

import json
import os
import signal
import sys
import time
from urllib.parse import urljoin

try:
    import requests
except ImportError:  # pragma: no cover - environment problem, not a code path
    sys.exit("This bridge needs the 'requests' package:  pip install requests")

FROZEN = getattr(sys, "frozen", False)

# Where to look for the settings file. For the packaged .exe that is the folder
# the operator put the .exe in; running from source it is the script's folder.
BASE_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
CONFIG_NAME = "ironcad-bridge.conf"
CONFIG_PATH = os.path.join(BASE_DIR, CONFIG_NAME)

CONFIG_TEMPLATE = """\
# IronCAD SDR bridge settings.
#
# Get both required values from IronCAD:
#   Settings -> Agency radio receiver -> Issue ingest key
# The key is shown once. If you lose it, rotate it there; that revokes the old
# one immediately.

IRONCAD_AGENCY_ID=
IRONCAD_INGEST_KEY=

# Optional.
# SITE_LABEL=Station 3 roof
# SDR_BASE_URL=http://127.0.0.1:5051
# IRONCAD_API=https://api.ironcad.tech
# POLL_SECONDS=10
"""


def read_config_file(path):
    """
    Read KEY=VALUE lines from the settings file beside the program.

    Environment variables still win, so an existing deployment that exports them
    behaves exactly as before and this file is simply never consulted. Anything
    unreadable is treated as absent rather than fatal: the operator gets the
    missing-settings message, which tells them what to do, instead of a stack
    trace about a file they may not know exists.
    """
    values = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        print(f"could not read {path}: {exc}", flush=True)
        return {}
    return values


FILE_CONFIG = read_config_file(CONFIG_PATH)


def setting(name, default=""):
    """Environment first, then the settings file, then the default."""
    from_env = os.environ.get(name)
    if from_env is not None and from_env.strip():
        return from_env.strip()
    return FILE_CONFIG.get(name, default).strip()


SDR_BASE = setting("SDR_BASE_URL", "http://127.0.0.1:5051").rstrip("/") + "/"
IRONCAD_API = setting("IRONCAD_API", "https://api.ironcad.tech").rstrip("/")
AGENCY_ID = setting("IRONCAD_AGENCY_ID")
INGEST_KEY = setting("IRONCAD_INGEST_KEY")
SITE_LABEL = setting("SITE_LABEL") or None
try:
    POLL_SECONDS = max(5, int(setting("POLL_SECONDS", "10")))
except ValueError:
    POLL_SECONDS = 10

# Read endpoints only. Nothing here can key a transmitter.
ENDPOINTS = {
    "aprs": "api/v1/aprs/stations",
    "aircraft": "api/v1/adsb/aircraft",
    "weather": "api/v1/weather/sensors",
}

LOCAL_TIMEOUT = 8
PUSH_TIMEOUT = 20

_running = True


def _stop(_signum, _frame):
    global _running
    _running = False
    print("stopping after the current cycle...", flush=True)


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


def get_local(path):
    """Read one appliance endpoint. A decoder that is off is not an error."""
    try:
        res = requests.get(urljoin(SDR_BASE, path), timeout=LOCAL_TIMEOUT)
        if res.status_code == 404:
            return None
        res.raise_for_status()
        return res.json()
    except Exception as exc:  # noqa: BLE001 - any local failure is just "no data"
        print(f"  local {path}: {exc}", flush=True)
        return None


def as_list(payload, *keys):
    """The appliance wraps lists differently per decoder; accept the shapes it uses."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in keys:
            v = payload.get(k)
            if isinstance(v, list):
                return v
        for v in payload.values():
            if isinstance(v, list):
                return v
    return []


def collect_packets(aprs_payload):
    """
    Pull RAW TNC2 packet text out of whatever the appliance returned.

    Sending raw text rather than the appliance's parsed stations is deliberate -
    see the module docstring. If a build exposes only parsed stations and no raw
    packets, nothing is sent for APRS rather than sending a weaker decode.
    """
    packets = []
    for row in as_list(aprs_payload, "packets", "stations", "results"):
        if isinstance(row, str):
            packets.append(row)
        elif isinstance(row, dict):
            raw = row.get("raw") or row.get("packet") or row.get("tnc2")
            if isinstance(raw, str) and ">" in raw and ":" in raw:
                packets.append(raw)
    return packets[:500]


def push(body):
    headers = {
        "Content-Type": "application/json",
        "X-IronCAD-Agency": AGENCY_ID,
        "Authorization": f"Bearer {INGEST_KEY}",
    }
    res = requests.post(
        f"{IRONCAD_API}/api/sdr/ingest",
        headers=headers,
        data=json.dumps(body),
        timeout=PUSH_TIMEOUT,
    )
    return res


def cycle():
    aprs = get_local(ENDPOINTS["aprs"])
    aircraft = get_local(ENDPOINTS["aircraft"])
    weather = get_local(ENDPOINTS["weather"])

    body = {
        "site": SITE_LABEL,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "aprs_packets": collect_packets(aprs),
        "aircraft": as_list(aircraft, "aircraft", "results"),
        "weather": as_list(weather, "sensors", "results"),
    }

    # An empty push is still worth sending: it is how IronCAD knows the receiver
    # is alive and hearing nothing, which is a different thing from a receiver
    # that has died. Suppressing it would make a healthy quiet night look like an
    # outage.
    res = push(body)
    if res.status_code == 401:
        print("  push rejected: check IRONCAD_AGENCY_ID and IRONCAD_INGEST_KEY", flush=True)
        return
    if res.status_code == 422:
        # The server refuses transmit payloads. Reaching this means the appliance
        # returned something this bridge should never have forwarded.
        print(f"  push refused: {res.text[:200]}", flush=True)
        return
    if not res.ok:
        print(f"  push failed {res.status_code}: {res.text[:200]}", flush=True)
        return

    counts = (res.json() or {}).get("counts", {})
    print(
        "  ok  aprs {heard} heard / {pos} positions, aircraft {ac}, weather {wx}".format(
            heard=counts.get("aprs_heard", 0),
            pos=counts.get("aprs_positions", 0),
            ac=counts.get("aircraft", 0),
            wx=counts.get("weather", 0),
        ),
        flush=True,
    )


def halt(message, code=1):
    """
    Stop with an explanation the operator can actually read.

    Double-clicking the packaged .exe opens a console window that closes the
    instant the process ends, so an error printed and exited normally is an
    error nobody sees - the program just appears to do nothing. When frozen,
    wait for a keypress first.
    """
    print(message, flush=True)
    if FROZEN:
        print("", flush=True)
        try:
            input("Press Enter to close...")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)


def main():
    if not AGENCY_ID or not INGEST_KEY:
        created = False
        if not os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
                    handle.write(CONFIG_TEMPLATE)
                created = True
            except OSError as exc:
                print(f"could not write {CONFIG_PATH}: {exc}", flush=True)
        lines = [
            "IronCAD SDR bridge is not configured yet.",
            "",
            "It needs your agency ID and an ingest key, both from IronCAD:",
            "  Settings -> Agency radio receiver -> Issue ingest key",
            "",
        ]
        if created:
            lines += [
                f"A settings file has been created for you at:",
                f"  {CONFIG_PATH}",
                "Open it, fill in the two values, save, and run this again.",
            ]
        else:
            lines += [
                f"Fill in the two values in:",
                f"  {CONFIG_PATH}",
                "then run this again. (Environment variables also work and take",
                "precedence over the file.)",
            ]
        halt("\n".join(lines))

    print(f"IronCAD SDR bridge - {SDR_BASE} -> {IRONCAD_API} every {POLL_SECONDS}s", flush=True)
    print("receive only; transmit apps are never read", flush=True)

    backoff = POLL_SECONDS
    while _running:
        started = time.time()
        try:
            cycle()
            backoff = POLL_SECONDS
        except requests.RequestException as exc:
            # A flapping uplink must not turn into a tight retry loop against a
            # rate-limited endpoint, so failures back off to a ceiling.
            print(f"  cycle failed: {exc}", flush=True)
            backoff = min(backoff * 2, 300)
        elapsed = time.time() - started
        for _ in range(int(max(1, backoff - elapsed))):
            if not _running:
                break
            time.sleep(1)

    print("stopped.", flush=True)


if __name__ == "__main__":
    main()

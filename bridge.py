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

RUNNING IT
    The packaged IronCAD-Bridge.exe opens a window (see ui.py). This module is
    the engine underneath that window, and it also runs on its own, which is what
    a service manager should use:

        pip install requests
        export IRONCAD_AGENCY_ID=...    # shown in IronCAD settings
        export IRONCAD_INGEST_KEY=...   # issued once in IronCAD settings
        python bridge.py

    Optional: SDR_BASE_URL (default http://127.0.0.1:5051), IRONCAD_API
    (default https://api.ironcad.tech), SITE_LABEL, POLL_SECONDS (default 10,
    minimum 5).

    Settings also come from ironcad-bridge.conf beside the program. Environment
    variables win over the file, so a service deployment that exports them is
    unaffected and never reads it.

THE WINDOW AND THE CONSOLE SHARE ONE LOOP
    run_loop() below is the only implementation of the poll cycle. The console
    path and the window both drive it and differ only in what they do with the
    events it emits. A second copy of this loop behind a GUI is a second copy
    that drifts, and the one that drifts is always the one nobody is watching.
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

DEFAULTS = {
    "IRONCAD_AGENCY_ID": "",
    "IRONCAD_INGEST_KEY": "",
    "SITE_LABEL": "",
    "SDR_BASE_URL": "http://127.0.0.1:5051",
    "IRONCAD_API": "https://api.ironcad.tech",
    "POLL_SECONDS": "10",
}

# Read endpoints only. Nothing here can key a transmitter.
ENDPOINTS = {
    "aprs": "api/v1/aprs/stations",
    "aircraft": "api/v1/adsb/aircraft",
    "weather": "api/v1/weather/sensors",
}

LOCAL_TIMEOUT = 8
PUSH_TIMEOUT = 20
MIN_POLL_SECONDS = 5


def read_config_file(path=CONFIG_PATH):
    """
    Read KEY=VALUE lines from the settings file beside the program.

    Anything unreadable is treated as absent rather than fatal: the operator gets
    the missing-settings message, which tells them what to do, instead of a stack
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
    except OSError:
        return {}
    return values


def write_config_file(values, path=CONFIG_PATH):
    """
    Write the settings file, keeping the explanatory header.

    Only non-empty optional values are written uncommented, so a file saved from
    the window still reads like the template a person would edit by hand.
    """
    lines = [
        "# IronCAD SDR bridge settings.",
        "#",
        "# Get both required values from IronCAD:",
        "#   Settings -> Agency radio receiver -> Issue ingest key",
        "# The key is shown once. If you lose it, rotate it there; that revokes",
        "# the old one immediately.",
        "#",
        "# Environment variables of the same names override this file.",
        "",
        f"IRONCAD_AGENCY_ID={values.get('IRONCAD_AGENCY_ID', '').strip()}",
        f"IRONCAD_INGEST_KEY={values.get('IRONCAD_INGEST_KEY', '').strip()}",
        "",
    ]
    for key in ("SITE_LABEL", "SDR_BASE_URL", "IRONCAD_API", "POLL_SECONDS"):
        val = str(values.get(key, "")).strip()
        if val and val != DEFAULTS[key]:
            lines.append(f"{key}={val}")
        else:
            lines.append(f"# {key}={val or DEFAULTS[key]}")
    lines.append("")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def build_config(overrides=None, path=CONFIG_PATH, env=None):
    """
    Resolve settings: explicit overrides, then environment, then the file,
    then defaults.

    Overrides come first so the window can run with what is on screen before the
    operator has saved it. Environment still beats the file, which is what keeps
    an existing service deployment behaving exactly as it did.
    """
    env = os.environ if env is None else env
    from_file = read_config_file(path)
    overrides = overrides or {}

    def pick(key):
        if key in overrides and str(overrides[key]).strip():
            return str(overrides[key]).strip()
        from_env = env.get(key)
        if from_env is not None and from_env.strip():
            return from_env.strip()
        if from_file.get(key, "").strip():
            return from_file[key].strip()
        return DEFAULTS[key]

    try:
        poll = max(MIN_POLL_SECONDS, int(pick("POLL_SECONDS")))
    except (TypeError, ValueError):
        poll = int(DEFAULTS["POLL_SECONDS"])

    return {
        "agency_id": pick("IRONCAD_AGENCY_ID"),
        "ingest_key": pick("IRONCAD_INGEST_KEY"),
        "site_label": pick("SITE_LABEL") or None,
        "sdr_base": pick("SDR_BASE_URL").rstrip("/") + "/",
        "api": pick("IRONCAD_API").rstrip("/"),
        "poll_seconds": poll,
    }


def is_configured(cfg):
    return bool(cfg.get("agency_id")) and bool(cfg.get("ingest_key"))


def get_local(cfg, path, emit):
    """Read one appliance endpoint. A decoder that is off is not an error."""
    try:
        res = requests.get(urljoin(cfg["sdr_base"], path), timeout=LOCAL_TIMEOUT)
        if res.status_code == 404:
            return None
        res.raise_for_status()
        return res.json()
    except Exception as exc:  # noqa: BLE001 - any local failure is just "no data"
        emit("error", f"  local {path}: {exc}")
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


def push(cfg, body):
    headers = {
        "Content-Type": "application/json",
        "X-IronCAD-Agency": cfg["agency_id"],
        "Authorization": f"Bearer {cfg['ingest_key']}",
    }
    return requests.post(
        f"{cfg['api']}/api/sdr/ingest",
        headers=headers,
        data=json.dumps(body),
        timeout=PUSH_TIMEOUT,
    )


def cycle(cfg, emit):
    """
    One poll of the appliance and one push to IronCAD.

    Returns the server's counts on success, or None. An empty push is still
    worth sending: it is how IronCAD knows the receiver is alive and hearing
    nothing, which is a different thing from a receiver that has died.
    Suppressing it would make a healthy quiet night look like an outage.
    """
    aprs = get_local(cfg, ENDPOINTS["aprs"], emit)
    aircraft = get_local(cfg, ENDPOINTS["aircraft"], emit)
    weather = get_local(cfg, ENDPOINTS["weather"], emit)

    body = {
        "site": cfg["site_label"],
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "aprs_packets": collect_packets(aprs),
        "aircraft": as_list(aircraft, "aircraft", "results"),
        "weather": as_list(weather, "sensors", "results"),
    }

    res = push(cfg, body)
    if res.status_code == 401:
        emit("error", "  push rejected: check the agency ID and the ingest key")
        return None
    if res.status_code == 422:
        # The server refuses transmit payloads. Reaching this means the appliance
        # returned something this bridge should never have forwarded.
        emit("error", f"  push refused: {res.text[:200]}")
        return None
    if not res.ok:
        emit("error", f"  push failed {res.status_code}: {res.text[:200]}")
        return None

    counts = (res.json() or {}).get("counts", {}) or {}
    emit(
        "ok",
        "  ok  aprs {heard} heard / {pos} positions, aircraft {ac}, weather {wx}".format(
            heard=counts.get("aprs_heard", 0),
            pos=counts.get("aprs_positions", 0),
            ac=counts.get("aircraft", 0),
            wx=counts.get("weather", 0),
        ),
    )
    return counts


def run_loop(cfg, emit, should_stop, on_counts=None, sleep=time.sleep):
    """
    The poll loop. The console and the window both drive this one function.

    emit(level, text)   level is info, ok, warn or error
    should_stop()       truthy to finish after the current cycle
    on_counts(counts)   called with the server's counts after a successful push,
                        and with None when a cycle failed, so a caller can show
                        live figures without parsing the log text
    """
    emit("info", f"IronCAD SDR bridge - {cfg['sdr_base']} -> {cfg['api']} every {cfg['poll_seconds']}s")
    emit("info", "receive only; transmit apps are never read")

    backoff = cfg["poll_seconds"]
    while not should_stop():
        started = time.time()
        try:
            counts = cycle(cfg, emit)
            backoff = cfg["poll_seconds"]
            if on_counts:
                on_counts(counts)
        except requests.RequestException as exc:
            # A flapping uplink must not turn into a tight retry loop against a
            # rate-limited endpoint, so failures back off to a ceiling.
            emit("error", f"  cycle failed: {exc}")
            backoff = min(backoff * 2, 300)
            if on_counts:
                on_counts(None)
        elapsed = time.time() - started
        for _ in range(int(max(1, backoff - elapsed))):
            if should_stop():
                break
            sleep(1)

    emit("info", "stopped.")


# --------------------------------------------------------------------------
# Console entry point. The packaged .exe opens the window instead; see ui.py.
# --------------------------------------------------------------------------

_stopped = False


def _signal_stop(_signum, _frame):
    global _stopped
    _stopped = True
    print("stopping after the current cycle...", flush=True)


def console_emit(level, text):
    print(text, flush=True)


LOG_NAME = "ironcad-bridge.log"
LOG_PATH = os.path.join(BASE_DIR, LOG_NAME)


def make_log_emit(path=LOG_PATH):
    """
    Emit to a file instead of the console.

    A windowed build has no stdout at all, so a station that falls back to
    headless there would otherwise run completely silently. Lines handed to an
    emit never carry the ingest key, so the log does not either.
    """

    def log_emit(level, text):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"{stamp} {level} {text}\n")
        except OSError:
            pass

    return log_emit


def halt(message, code=1):
    """
    Stop with an explanation the operator can actually read.

    Double-clicking a console build opens a window that closes the instant the
    process ends, so an error printed and exited normally is an error nobody
    sees - the program just appears to do nothing. When frozen, wait for a
    keypress first.
    """
    print(message, flush=True)
    if FROZEN and sys.stdin is not None:
        print("", flush=True)
        try:
            input("Press Enter to close...")
        except (EOFError, KeyboardInterrupt, RuntimeError, OSError):
            pass
    sys.exit(code)


def unconfigured_message():
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
            "A settings file has been created for you at:",
            f"  {CONFIG_PATH}",
            "Open it, fill in the two values, save, and run this again.",
        ]
    else:
        lines += [
            "Fill in the two values in:",
            f"  {CONFIG_PATH}",
            "then run this again. (Environment variables also work and take",
            "precedence over the file.)",
        ]
    return "\n".join(lines)


def main(emit=None, report=None):
    """
    Run the bridge without a window.

    emit and report default to the console. A windowed build has no console, so
    the entry point passes file-backed versions instead; everything else about
    this path is identical either way.
    """
    signal.signal(signal.SIGINT, _signal_stop)
    signal.signal(signal.SIGTERM, _signal_stop)

    emit = emit or console_emit
    report = report or halt

    cfg = build_config()
    if not is_configured(cfg):
        report(unconfigured_message())
        return

    run_loop(cfg, emit, lambda: _stopped)


if __name__ == "__main__":
    main()

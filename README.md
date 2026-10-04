# IronCAD SDR bridge

Pushes what your radio receiver hears into IronCAD. Runs on the machine beside
the receiver, on your own network.

## What it does

Every few seconds it reads three receive decoders from a local SDR appliance
([hydro13/sdr](https://github.com/hydro13/sdr), MIT) and POSTs the result to
IronCAD over HTTPS:

| From the appliance | Becomes, in IronCAD |
|---|---|
| APRS packets (raw TNC2 text) | responder positions on the live map, in `tak_tracks` |
| Local ADS-B | aircraft on Area Watch, preferred over the internet feeds while fresh |
| 433 MHz weather sensors | local conditions at the incident |

**Why this matters:** APRS and aircraft normally reach IronCAD over the internet,
and the internet is the thing that goes away in the incidents this software is
for. Your own receiver hears your people directly off the air with nothing in
between.

## Receive only

The upstream appliance can also transmit — including POCSAG paging. This bridge
does not read those apps and will not send them, and the IronCAD server
independently refuses any push carrying transmit intent.

That is not caution for its own sake. Transmitting on public-safety pager
frequencies requires authority under the licence for those frequencies and
hardware type-accepted for the service; a HackRF driven from dispatch software
is neither. Amateur APRS transmit carries its own licence conditions and
prohibits business use. **Do not wire a transmitter to this.**

Receiving is the opposite case: ADS-B, APRS and weather telemetry are broadcast
unencrypted and intended to be received.

## Setup

1. In IronCAD: **Settings → Agency radio receiver → Issue ingest key.** Copy the
   key when it is shown — it is not shown again. Note your agency ID from the
   same screen.
2. On the machine beside the receiver, either run the window or run it headless.

### With the window

Download **IronCAD-Bridge.exe** and run it. Type the agency ID and the ingest
key into the settings panel, press **Save settings**, then press **Start**. The
status strip shows what the receiver is hearing and when IronCAD last accepted
a push. Settings are written to `ironcad-bridge.conf` beside the .exe, so the
machine is commissioned once and the file is what it runs from afterwards.

The window is for commissioning and for checking a station someone is standing
at. A station that should come back by itself after a power cut runs headless.

### Headless

**IronCAD-Bridge-Console.exe** is the same program built with a console, which
is what a service manager wants:

```
IronCAD-Bridge-Console.exe --no-ui
```

It reads the same `ironcad-bridge.conf`, and environment variables outrank the
file. `IronCAD-Bridge.exe --no-ui` also runs headless, but a windowed build has
no console on Windows, so it writes to `ironcad-bridge.log` beside the .exe
instead of to the screen.

### From source

```bash
pip install requests

export IRONCAD_AGENCY_ID="your-agency-id"
export IRONCAD_INGEST_KEY="the key you just copied"
export SITE_LABEL="Station 3 roof"      # optional, shown on the dashboard

python main.py            # window
python main.py --no-ui    # headless
```

You should see a line per cycle:

```
  ok  aprs 14 heard / 2 positions, aircraft 37, weather 1
```

"Heard" is every station on the air. "Positions" is how many of those matched a
callsign on your roster and became a pin — a receiver in a busy area hears
plenty of people who are not yours.

## Settings

| Variable | Default | Notes |
|---|---|---|
| `IRONCAD_AGENCY_ID` | — | required |
| `IRONCAD_INGEST_KEY` | — | required |
| `SDR_BASE_URL` | `http://127.0.0.1:5051` | the appliance's local API |
| `IRONCAD_API` | `https://api.ironcad.tech` | |
| `SITE_LABEL` | none | where the antenna is |
| `POLL_SECONDS` | `10` | minimum 5 |

Precedence is environment, then `ironcad-bridge.conf`, then these defaults. In
the window, a value coming from the environment is shown in its own field and
locked, so what is on screen is what the bridge is running; editing it there
would have no effect, and **Save settings** leaves it out of the file rather
than turning a machine-level setting into a stored one.

## Why the bridge sends a time with each packet

A receiver keeps reporting a station it is still listing, so the same beacon is
read on every poll. Until 1.2.0 the bridge sent bare packet text and the server
stamped each arrival with the current time, which made one transmission look
like a continuous stream of new positions. A responder who stopped transmitting
went on showing a current position on the map, and the age shown beside their
name read as seconds old however long ago they had actually keyed up.

From 1.2.0 the bridge remembers when it first saw each packet and sends that
time alongside it, preferring the appliance's own heard time when it reports
one. The server uses that for the position's timestamp, so a re-reported beacon
no longer refreshes the pin and a station that goes quiet ages out properly.

**Upgrade the bridge to get this.** A 1.0.0 or 1.1.0 bridge sends bare text, and
the server has nothing better to go on than arrival time, so the old behaviour
continues until the bridge is replaced.

## Linking a responder to a callsign

A packet only becomes a pin if its callsign is on the roster. Set each member's
APRS callsign in their IronCAD profile, including the SSID (`N0CALL-9`).

Known gap: Mic-E position reports are not decoded yet. Most trackers can be set
to send standard-format beacons instead.

## If nothing arrives

The settings card tells three states apart, and the difference matters:

- **No receiver connected** — no key has been issued.
- **Key issued, nothing has arrived** — the bridge has never reached IronCAD.
  Check it is running and can reach `api.ironcad.tech` on 443.
- **Receiver has gone quiet** — it was working and stopped. Positions are
  withheld rather than drawn as current, because a two-minute-old position is
  not where the symbol says it is.

`401` from the push means the agency ID or the key is wrong. Rotate the key in
IronCAD if it has been lost — that revokes the old one immediately.

## Attribution

The appliance is [hydro13/sdr](https://github.com/hydro13/sdr), MIT licensed.
This bridge is part of IronCAD and talks to its public HTTP API; no upstream
code is reused.

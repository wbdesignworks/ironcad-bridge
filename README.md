# IronCAD Bridge

Pushes what your radio receiver hears into IronCAD: APRS positions, locally
received ADS-B aircraft, and 433 MHz weather-sensor telemetry. It runs on the
machine beside the receiver, on your own network.

This is NOT the IronCAD Windows app. The Windows app is the dispatch client an
operator works in. This Bridge is the piece that talks to the radio hardware and
pushes what it hears up to IronCAD. They are installed separately and a station
may well run both on the same machine.

## Install

1. Put `IronCAD-Bridge.exe` anywhere on the machine next to your receiver.
   No Python and no other dependencies are required.
2. Double-click it once. It writes `ironcad-bridge.conf` beside itself and
   tells you what is missing.
3. In IronCAD: **Settings -> Agency radio receiver -> Issue ingest key.** Copy
   the key when it is shown; it is not shown again. Note your agency ID from the
   same screen.
4. Put both values into `ironcad-bridge.conf` and save.
5. Run it again. You should see a line per cycle:

       ok  aprs 14 heard / 2 positions, aircraft 37, weather 1

   "Heard" is every station on the air. "Positions" is how many matched a
   callsign on your roster and became a pin.

Environment variables of the same names also work and take precedence over the
file, which is how you would run it under a service manager.

## Receive only

This Bridge reads three receive decoders and nothing else. It does not read or
operate the appliance's transmit apps, and the IronCAD server independently
refuses any push carrying transmit intent.

Transmitting on public-safety or amateur frequencies requires authority under
the licence for those frequencies and hardware type-accepted for the service.
Do not wire a transmitter to this.

## Connecting a responder to a callsign

A packet only becomes a pin if its callsign is on the roster. Set each member's
APRS callsign, including the SSID (`N0CALL-9`), in their IronCAD profile.

## If nothing arrives

The settings card in IronCAD tells three states apart:

- **No receiver connected** — no key has been issued.
- **Key issued, nothing has arrived** — the Bridge has never reached IronCAD.
  Check it is running and can reach `api.ironcad.tech` on 443.
- **Receiver has gone quiet** — it was working and stopped. Positions are
  withheld rather than drawn as current.

A `401` means the agency ID or the key is wrong.

## This build

    IronCAD-Bridge.exe   16,542,816 bytes
    sha256               62a2b863d08f535108d4fb0b1f759a00f8b6049cad8bbf68f6dc4dd0cd8a1748

Unsigned, like the IronCAD Windows app. Windows SmartScreen will warn on first
run until code signing is in place.

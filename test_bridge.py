"""
Tests for the bridge engine.

The refactor that put a window on this program moved configuration and the poll
loop into functions the window also calls. These tests exist because the
headless path is the one nobody is watching: if precedence or the loop breaks,
a station comes back from a power cut subtly wrong and nothing says so.

    python -m pytest test_bridge.py        (or: python test_bridge.py)
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge  # noqa: E402

# Several tests replace module-level functions and do not put them back. Hold
# the real ones from before any of that happens, so a test that needs the real
# cycle gets the real cycle regardless of what ran first.
REAL = {
    "cycle": bridge.cycle,
    "build_config": bridge.build_config,
    "is_configured": bridge.is_configured,
    "get_local": bridge.get_local,
    "push": bridge.push,
}


def restore_real():
    for name, func in REAL.items():
        setattr(bridge, name, func)


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise AssertionError("raise_for_status on a failed response")


class ConfigPrecedence(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".conf")
        os.close(fd)

    def tearDown(self):
        os.unlink(self.path)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_file_is_used_when_nothing_else_is_set(self):
        self.write("IRONCAD_AGENCY_ID=from-file\nIRONCAD_INGEST_KEY=k\n")
        cfg = bridge.build_config(path=self.path, env={})
        self.assertEqual(cfg["agency_id"], "from-file")

    def test_environment_beats_the_file(self):
        # This is the one that keeps existing service deployments working.
        self.write("IRONCAD_AGENCY_ID=from-file\nIRONCAD_INGEST_KEY=k\n")
        cfg = bridge.build_config(path=self.path, env={"IRONCAD_AGENCY_ID": "from-env"})
        self.assertEqual(cfg["agency_id"], "from-env")

    def test_overrides_beat_the_environment(self):
        # The window runs with what is on screen before it has been saved.
        self.write("IRONCAD_AGENCY_ID=from-file\nIRONCAD_INGEST_KEY=k\n")
        cfg = bridge.build_config(
            {"IRONCAD_AGENCY_ID": "from-form"},
            path=self.path,
            env={"IRONCAD_AGENCY_ID": "from-env"},
        )
        self.assertEqual(cfg["agency_id"], "from-form")

    def test_blank_values_do_not_count_as_set(self):
        self.write("IRONCAD_AGENCY_ID=from-file\nIRONCAD_INGEST_KEY=k\n")
        cfg = bridge.build_config({"IRONCAD_AGENCY_ID": "   "}, path=self.path,
                                  env={"IRONCAD_AGENCY_ID": ""})
        self.assertEqual(cfg["agency_id"], "from-file")

    def test_defaults_fill_the_rest(self):
        cfg = bridge.build_config(path=self.path, env={})
        self.assertEqual(cfg["api"], "https://api.ironcad.tech")
        self.assertEqual(cfg["sdr_base"], "http://127.0.0.1:5051/")
        self.assertEqual(cfg["poll_seconds"], 10)

    def test_poll_floor_and_garbage(self):
        self.assertEqual(bridge.build_config({"POLL_SECONDS": "1"}, path=self.path, env={})["poll_seconds"], 5)
        self.assertEqual(bridge.build_config({"POLL_SECONDS": "x"}, path=self.path, env={})["poll_seconds"], 10)

    def test_trailing_slashes_are_normalised(self):
        cfg = bridge.build_config({"IRONCAD_API": "https://x.test/", "SDR_BASE_URL": "http://y.test"},
                                  path=self.path, env={})
        self.assertEqual(cfg["api"], "https://x.test")
        self.assertEqual(cfg["sdr_base"], "http://y.test/")

    def test_is_configured_needs_both(self):
        self.assertFalse(bridge.is_configured({"agency_id": "a", "ingest_key": ""}))
        self.assertFalse(bridge.is_configured({"agency_id": "", "ingest_key": "k"}))
        self.assertTrue(bridge.is_configured({"agency_id": "a", "ingest_key": "k"}))

    def test_round_trip_through_the_written_file(self):
        bridge.write_config_file(
            {"IRONCAD_AGENCY_ID": "a1", "IRONCAD_INGEST_KEY": "k1",
             "SITE_LABEL": "Station 3 roof", "POLL_SECONDS": "30",
             "SDR_BASE_URL": "http://10.0.0.5:5051", "IRONCAD_API": "https://api.ironcad.tech"},
            path=self.path,
        )
        cfg = bridge.build_config(path=self.path, env={})
        self.assertEqual(cfg["agency_id"], "a1")
        self.assertEqual(cfg["ingest_key"], "k1")
        self.assertEqual(cfg["site_label"], "Station 3 roof")
        self.assertEqual(cfg["poll_seconds"], 30)
        self.assertEqual(cfg["sdr_base"], "http://10.0.0.5:5051/")

    def test_written_file_does_not_leave_a_default_uncommented(self):
        bridge.write_config_file({"IRONCAD_AGENCY_ID": "a", "IRONCAD_INGEST_KEY": "k"}, path=self.path)
        with open(self.path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("# IRONCAD_API=https://api.ironcad.tech", body)
        self.assertIn("IRONCAD_AGENCY_ID=a", body)


class CycleBehaviour(unittest.TestCase):
    """The parts that decide what reaches IronCAD, and what the operator is told."""

    def setUp(self):
        self.cfg = {"agency_id": "a", "ingest_key": "k", "site_label": "roof",
                    "sdr_base": "http://local/", "api": "https://api.test",
                    "poll_seconds": 5}
        self.events = []
        self.emit = lambda level, text: self.events.append((level, text))

    def levels(self):
        return [lv for lv, _ in self.events]

    def test_empty_push_is_still_sent(self):
        # A quiet night must not look like an outage.
        sent = {}
        bridge.get_local = lambda cfg, path, emit: None
        bridge.push = lambda cfg, body: sent.update(body) or FakeResponse(200, {"counts": {}})
        counts = bridge.cycle(self.cfg, self.emit)
        self.assertEqual(counts, {})
        self.assertIn("observed_at", sent)
        self.assertEqual(sent["aprs_packets"], [])

    def test_401_is_reported_as_credentials_not_a_generic_failure(self):
        bridge.get_local = lambda cfg, path, emit: None
        bridge.push = lambda cfg, body: FakeResponse(401)
        self.assertIsNone(bridge.cycle(self.cfg, self.emit))
        self.assertIn("error", self.levels())
        self.assertTrue(any("agency ID" in t for _, t in self.events))

    def test_422_surfaces_the_server_refusal(self):
        bridge.get_local = lambda cfg, path, emit: None
        bridge.push = lambda cfg, body: FakeResponse(422, text="transmit intent refused")
        self.assertIsNone(bridge.cycle(self.cfg, self.emit))
        self.assertTrue(any("refused" in t for _, t in self.events))

    def test_success_emits_counts_and_an_ok_line(self):
        bridge.get_local = lambda cfg, path, emit: None
        bridge.push = lambda cfg, body: FakeResponse(
            200, {"counts": {"aprs_heard": 14, "aprs_positions": 2, "aircraft": 37, "weather": 1}})
        counts = bridge.cycle(self.cfg, self.emit)
        self.assertEqual(counts["aprs_heard"], 14)
        self.assertIn("ok", self.levels())
        self.assertTrue(any("14 heard / 2 positions" in t for _, t in self.events))

    def test_ingest_key_never_appears_in_any_emitted_line(self):
        # The log pane and the log file are both built from these events.
        self.cfg["ingest_key"] = "SUPER-SECRET-KEY"
        bridge.get_local = lambda cfg, path, emit: None
        bridge.push = lambda cfg, body: FakeResponse(401)
        bridge.cycle(self.cfg, self.emit)
        self.assertFalse(any("SUPER-SECRET-KEY" in t for _, t in self.events))


class CollectPackets(unittest.TestCase):
    def test_takes_raw_text_and_rejects_parsed_only_rows(self):
        payload = {"packets": [
            "N0CALL-9>APRS,TCPIP*:!4903.50N/07201.75W-",
            {"raw": "W1AW>APRS,TCPIP*:!4203.10N/07132.20W-"},
            {"callsign": "K1ABC", "lat": 39.1},          # parsed only - dropped
            {"raw": "not a packet"},                      # no > and : - dropped
        ]}
        got = bridge.collect_packets(payload)
        self.assertEqual(len(got), 2)
        self.assertTrue(all(">" in p and ":" in p for p in got))

    def test_caps_at_five_hundred(self):
        payload = {"packets": ["A>B:c"] * 900}
        self.assertEqual(len(bridge.collect_packets(payload)), 500)

    def test_as_list_accepts_the_shapes_the_appliance_uses(self):
        self.assertEqual(bridge.as_list(None), [])
        self.assertEqual(bridge.as_list([1, 2]), [1, 2])
        self.assertEqual(bridge.as_list({"aircraft": [1]}, "aircraft"), [1])
        self.assertEqual(bridge.as_list({"anything": [7]}), [7])


class LoopControl(unittest.TestCase):
    def test_stops_without_running_a_cycle_when_already_stopped(self):
        events = []
        bridge.cycle = lambda cfg, emit: events.append("cycled")
        cfg = {"sdr_base": "x/", "api": "y", "poll_seconds": 5}
        bridge.run_loop(cfg, lambda lv, t: None, lambda: True, sleep=lambda s: None)
        self.assertNotIn("cycled", events)

    def test_runs_then_stops_and_says_so(self):
        calls = {"n": 0}

        def fake_cycle(cfg, emit):
            calls["n"] += 1
            return {}

        bridge.cycle = fake_cycle
        said = []
        cfg = {"sdr_base": "x/", "api": "y", "poll_seconds": 5}
        bridge.run_loop(cfg, lambda lv, t: said.append(t),
                        lambda: calls["n"] >= 2, sleep=lambda s: None)
        self.assertEqual(calls["n"], 2)
        self.assertIn("stopped.", said)
        self.assertTrue(any("receive only" in s for s in said))


class WindowlessOutput(unittest.TestCase):
    """
    A windowed build has no stdout. These cover the substitute, because a
    station that runs correctly and reports nothing is the failure that takes
    longest to notice.
    """

    def setUp(self):
        restore_real()
        fd, self.path = tempfile.mkstemp(suffix=".log")
        os.close(fd)

    def tearDown(self):
        restore_real()
        os.unlink(self.path)

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def test_log_emit_writes_the_line_with_a_timestamp_and_level(self):
        emit = bridge.make_log_emit(self.path)
        emit("warn", "window unavailable")
        written = self.read()
        self.assertIn("warn window unavailable", written)
        self.assertRegex(written, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")

    def test_log_emit_appends_rather_than_truncating(self):
        emit = bridge.make_log_emit(self.path)
        emit("info", "first")
        emit("info", "second")
        self.assertIn("first", self.read())
        self.assertIn("second", self.read())

    def test_log_emit_survives_an_unwritable_path(self):
        emit = bridge.make_log_emit(os.path.join(self.path, "nope", "x.log"))
        emit("info", "swallowed")  # must not raise

    def test_the_ingest_key_never_reaches_the_log(self):
        key = "ik_live_do_not_log_me"
        cfg = {
            "agency_id": "A1",
            "ingest_key": key,
            "site_label": "",
            "sdr_base": "http://127.0.0.1:5051/",
            "api": "https://api.example.test",
            "poll_seconds": 5,
        }
        bridge.requests.get = lambda *a, **k: FakeResponse(401, text=key)
        bridge.requests.post = lambda *a, **k: FakeResponse(401, text=key)
        emit = bridge.make_log_emit(self.path)
        bridge.cycle(cfg, emit)
        written = self.read()
        # Assert the cycle actually reported something first. Without this the
        # absence of the key is satisfied by an empty log, which is what a
        # stubbed-out cycle leaves behind.
        self.assertIn("push rejected", written)
        self.assertNotIn(key, written)

    def test_unconfigured_reports_instead_of_exiting_when_given_a_report(self):
        reported = []
        bridge.build_config = lambda: dict(bridge.DEFAULTS, agency_id="",
                                           ingest_key="")
        bridge.is_configured = lambda cfg: False
        bridge.main(emit=lambda lv, t: None, report=reported.append)
        self.assertEqual(len(reported), 1)
        self.assertIn("not configured", reported[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)

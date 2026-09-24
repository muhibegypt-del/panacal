from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from autocal import open_hardware
from domain import XYZ
from hardware import Meter

FIXTURE = Path(__file__).with_name("spotread_fixture.py")

class Log:
    def __init__(self):
        self.rows = []
    def write(self, message):
        self.rows.append(message)

class MeterPipeTests(unittest.TestCase):
    def meter(self, scenario, **kwargs):
        return Meter(Path(sys.executable), ["-u", str(FIXTURE), scenario], Log(),
                     startup_timeout=kwargs.pop("startup_timeout", 5),
                     read_timeout=kwargs.pop("read_timeout", 3), **kwargs)

    def assert_closed(self, meter, process):
        self.assertIsNotNone(process.poll())
        self.assertFalse(meter.reader_thread.is_alive())
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)

    def test_split_prompt_without_newline_and_three_persistent_reads(self):
        meter = self.meter("normal")
        process = meter.process
        try:
            for number in range(1, 4):
                self.assertEqual(meter.read(), XYZ(number, number+1, number+2))
                self.assertEqual(meter.process.pid, process.pid)
                self.assertTrue(meter.ready)
                self.assertIn(Meter.READY_PROMPT, meter.log.rows[-1])
        finally:
            meter.close()
        self.assert_closed(meter, process)
        meter.close()

    def test_argyll_quit_confirmation_exits_normally(self):
        meter = self.meter("confirm_quit")
        process = meter.process
        meter.close()
        self.assert_closed(meter, process)
        self.assertEqual(process.returncode, 0)

    def test_constructor_timeout_reaps_the_child(self):
        children = []
        popen = subprocess.Popen
        def capture(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child
        with patch("hardware.subprocess.Popen", side_effect=capture):
            with self.assertRaisesRegex(TimeoutError, "startup timed out"):
                self.meter("startup_hang", startup_timeout=0.5)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)

    def test_startup_eof_includes_unterminated_error_output(self):
        with self.assertRaisesRegex(RuntimeError, "USB port unavailable: fixture diagnostic"):
            self.meter("startup_exit")

    def test_read_timeout_reaps_the_child(self):
        meter = self.meter("read_hang", read_timeout=0.3)
        process = meter.process
        with self.assertRaisesRegex(TimeoutError, "measurement timed out"):
            meter.read()
        self.assert_closed(meter, process)

    def test_prompt_without_result_is_not_a_successful_read(self):
        meter = self.meter("no_result")
        process = meter.process
        with self.assertRaisesRegex(RuntimeError, "without an XYZ result"):
            meter.read()
        self.assert_closed(meter, process)

    def test_nonfinite_result_rejected(self):
        meter = self.meter("nonfinite")
        process = meter.process
        with self.assertRaisesRegex(RuntimeError, "Non-finite XYZ"):
            meter.read()
        self.assert_closed(meter, process)

    def test_refresh_failure_is_not_accepted(self):
        meter = self.meter("refresh_failure")
        process = meter.process
        with self.assertRaisesRegex(RuntimeError, "Measuring refresh rate failed"):
            meter.read()
        self.assert_closed(meter, process)

    def test_eof_after_result_does_not_leave_a_dead_session_ready(self):
        meter = self.meter("exit_after_result")
        process = meter.process
        with self.assertRaisesRegex(RuntimeError, "exited during measurement"):
            meter.read()
        self.assert_closed(meter, process)

class SetupOrderTests(unittest.TestCase):
    def test_meter_startup_failure_precedes_any_picture_write(self):
        config = json.loads((ROOT / "config.json").read_text())
        pattern, tv = MagicMock(), MagicMock()
        with patch("autocal.PatternHost", return_value=pattern), \
             patch("autocal.connect_tv", return_value=tv), \
             patch("autocal.Meter", side_effect=TimeoutError("meter unavailable")), \
             patch("builtins.input", return_value=""):
            with self.assertRaisesRegex(TimeoutError, "meter unavailable"):
                open_hardware(ROOT, Log(), config, "unused", Path("spotread.exe"), prepare=True)
        tv.snapshot.assert_not_called()
        tv.apply_picture.assert_not_called()
        tv.reset_calibration.assert_not_called()
        pattern.linearize_video_lut.assert_not_called()
        tv.close.assert_called_once()
        pattern.close.assert_called_once()

    def test_startup_prepares_white_without_a_discarded_measurement(self):
        config = json.loads((ROOT / "config.json").read_text())
        events = []
        pattern, tv, meter = MagicMock(), MagicMock(), MagicMock()
        def ready(*_):
            events.append("ready")
            return meter
        pattern.linearize_video_lut.side_effect = lambda *_: events.append("lut")
        tv.apply_picture.side_effect = lambda *_: events.append("picture")
        tv.reset_calibration.side_effect = lambda: events.append("reset")
        tv.snapshot.return_value = {
            "picture": {"PC:BRI": -5, "PC:CON": 48, "PC:TMP": "WRM2", "PC:GMM": "2.2"},
            "two_point": {"WB:HIR": 8},
            "detail": {"10": {"WB:GNR": -3}},
        }
        with patch("autocal.PatternHost", return_value=pattern), \
             patch("autocal.connect_tv", return_value=tv), \
             patch("autocal.Meter", side_effect=ready), \
             patch("builtins.input", return_value=""), patch("autocal.save_json"):
            actual = open_hardware(ROOT, Log(), config, "unused", Path("spotread.exe"), prepare=True)
        self.assertIs(actual[2], meter)
        self.assertEqual(events, ["ready", "lut"])
        tv.apply_picture.assert_not_called()
        tv.reset_calibration.assert_not_called()
        tv.snapshot.assert_called_once()
        self.assertIs(actual[3], tv.snapshot.return_value)
        pattern.start.assert_called_once_with(100)
        meter.read.assert_not_called()

if __name__ == "__main__":
    unittest.main()

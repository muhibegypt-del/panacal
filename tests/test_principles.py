"""Guards, edge cases, error paths and clean-up, one check per behaviour.

Run with: python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from lgcal import app, settings as settings_file, setup
from lgcal.display import DisplaySetup
from lgcal.lg import LG
from lgcal.server import MeterService, is_synthetic_black
from lgcal.signal import (Reader, classify_range, on_patch, steady_white, summarize, verification_row,
                          wait_for_meter)


def quiet(call, *args, **kwargs):
    with redirect_stdout(io.StringIO()) as out:
        result = call(*args, **kwargs)
    return result, out.getvalue()


class SettingsGuards(unittest.TestCase):
    def test_no_file_means_defaults(self):
        settings, warnings = settings_file.load(Path("/nonexistent/settings.json"))
        self.assertEqual((settings["target_gamma"], settings["patch_size"], warnings), ("bt1886", 10, []))

    def test_every_problem_is_reported_at_once(self):
        with self.assertRaises(ValueError) as caught:
            settings_file.validate({"target_gamma": "2.6", "patch_size": 0, "target_delta_e": "0.5",
                                    "tv_ip": "192.168.1", "picture_mode": "ISF", "reset_picture_mode": "yes",
                                    "meter": {"ccss": "C:/missing.ccss", "args": "-e"}})
        message = str(caught.exception)
        for key in ("target_gamma", "patch_size", "target_delta_e", "tv_ip", "picture_mode",
                    "reset_picture_mode", "meter.ccss", "meter.args"):
            self.assertIn(key, message)

    def test_typos_are_named_not_ignored(self):
        _, warnings = settings_file.validate({"target_gama": "2.2", "meter": {"cccs": ""}, "_comment": "x"})
        self.assertIn("did you mean 'target_gamma'", warnings[0])
        self.assertIn("did you mean 'meter.ccss'", warnings[1])
        self.assertEqual(len(warnings), 2)

    def test_values_are_normalised(self):
        settings, _ = settings_file.validate({"target_gamma": "BT1886", "patch_size": 18.0})
        self.assertEqual((settings["target_gamma"], settings["patch_size"]), ("bt1886", 18))

    def test_not_an_object_or_not_json(self):
        with self.assertRaises(ValueError):
            settings_file.validate(["target_gamma"])
        directory = Path(tempfile.mkdtemp())
        try:
            (directory / "settings.json").write_text("{ target_gamma: 2.2 }")
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                settings_file.load(directory / "settings.json")
        finally:
            shutil.rmtree(directory)

    def test_launcher_stops_with_the_list(self):
        directory = Path(tempfile.mkdtemp())
        try:
            (directory / "settings.json").write_text('{"patch_size": 500}')
            with self.assertRaisesRegex(SystemExit, "patch_size"):
                quiet(app.load_settings, directory / "settings.json")
        finally:
            shutil.rmtree(directory)


class PureDecisions(unittest.TestCase):
    def test_range_classification(self):
        self.assertEqual(classify_range(0.0, 0.73, 145), "full")        # 0.5% of white
        self.assertEqual(classify_range(0.0, 0.09, 145), "limited")     # 0.06% of white
        self.assertEqual(classify_range(0.30, 2.0, 120), "lifted")
        with self.assertRaises(ValueError):
            classify_range(0, 0, 0)

    def test_meter_placement(self):
        self.assertFalse(steady_white(None, 140))
        self.assertFalse(steady_white(140, 5))            # too dim to be the patch
        self.assertFalse(steady_white(100, 140))          # still settling
        self.assertTrue(steady_white(139, 140))
        self.assertTrue(on_patch(140, 0.0))
        self.assertFalse(on_patch(140, 60))               # room light does not follow the patch
        self.assertFalse(on_patch(0, 0))

    def test_verification_rows(self):
        white = (95.047, 100.0, 108.883)
        row = verification_row(100, white, 100.0, "2.2", False)
        self.assertLess(row["de"], 0.05)          # D65 XYZ vs D65 xy rounding
        self.assertEqual(verification_row(50, white, 100.0, "2.2", True)["code"], 126)
        with self.assertRaises(ValueError):
            summarize([])

    def test_synthetic_black(self):
        self.assertTrue(is_synthetic_black({"patch_r": 16, "patch_g": 16, "patch_b": 16, "ire": 0}, True))
        self.assertFalse(is_synthetic_black({"patch_r": 16, "patch_g": 16, "patch_b": 16, "ire": 0}, False))
        self.assertFalse(is_synthetic_black({"patch_r": 0, "patch_g": 0, "patch_b": 1, "ire": 0}, True))
        self.assertTrue(is_synthetic_black({"patch_r": 0, "patch_g": 0, "patch_b": 0}, True))

    def test_download_choices(self):
        releases = [{"archname": "MSWin32-x64-multi-thread", "edition": {
            "msi": {"url": "https://x/strawberry-perl-5.40.5.1-64bit.msi"},
            "portable": {"url": "https://x/strawberry-perl-5.40.5.1-64bit.msi"},
            "pdl": {"url": "https://x/strawberry-perl-5.40.5.1-64bit-portable.zip", "sha256": "ab"}}}]
        self.assertEqual(setup.portable_zip(releases),
                         ("https://x/strawberry-perl-5.40.5.1-64bit-portable.zip", {"ab"}))
        self.assertEqual(setup.portable_zip([]), ("", set()))
        self.assertEqual(setup.portable_zip("garbage"), ("", set()))

    def test_real_releases_json_shuffle_accepts_the_real_zip(self):
        # strawberryperl.com releases.json, Sep 2026 / 5.40.5.1, as published:
        # the portable zip's real checksum (6619fe7e..., 304,297,789 bytes,
        # verified by download) sits beside the MSI's URL.
        release = [{"archname": "MSWin32-x64-multi-thread", "edition": {
            "msi": {"url": "https://g/strawberry-perl-5.40.5.1-64bit.msi", "sha256": "dcd84b77"},
            "pdl": {"url": "https://g/strawberry-perl-5.40.5.1-64bit-portable.zip", "sha256": "3180b743"},
            "portable": {"url": "https://g/strawberry-perl-5.40.5.1-64bit.msi", "sha256": "6619FE7E"}}}]
        url, accepted = setup.portable_zip(release)
        self.assertTrue(url.endswith("-portable.zip"))
        self.assertIn("6619fe7e", accepted)

    def test_download_checks_against_accepted_checksums(self):
        import hashlib
        body = b"strawberry"
        good = hashlib.sha256(body).hexdigest()

        class Response(io.BytesIO):
            headers = {"Content-Length": str(len(body))}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        with mock.patch("urllib.request.urlopen", return_value=Response(body)):
            setup.download("https://x/a.zip", directory / "a.zip", lambda _m: None, {"0000", good.upper()})
        self.assertEqual((directory / "a.zip").read_bytes(), body)
        with mock.patch("urllib.request.urlopen", return_value=Response(body)):
            with self.assertRaisesRegex(SystemExit, "damaged"):
                setup.download("https://x/b.zip", directory / "b.zip", lambda _m: None, {"0000"})
        self.assertFalse((directory / "b.zip").exists())
        page = ('<a href="https://www.argyllcms.com/Argyll_V3.4.0_win64_exe.zip">'
                '<a href="https://www.argyllcms.com/Argyll_V3.5.0_win64_exe.zip">')
        self.assertTrue(setup.latest_argyll_link(page).endswith("V3.5.0_win64_exe.zip"))
        self.assertEqual(setup.latest_argyll_link(""), "")

    def test_only_home_networks_are_swept(self):
        from lgcal.discover import private_networks
        networks = private_networks(["192.168.1.20", "127.0.0.1", "169.254.1.1", "8.8.8.8", "bad",
                                     "192.168.1.99"])
        self.assertEqual([str(n) for n in networks], ["192.168.1.0/24"])
        self.assertEqual(private_networks([]), [])

    def test_progress_without_a_size(self):
        self.assertEqual(setup.progress_step(50, 100), 5)
        self.assertEqual(setup.progress_step(25 << 20, 0), 2)   # every 10 MB when the size is unknown

    def test_progress_line_tolerates_junk(self):
        self.assertEqual(app.progress_line({}, 5), "")
        self.assertIn("50%", app.progress_line({"current_name": "sdr26_50%", "current_step": "x",
                                                "total_steps": None}, 5))
        self.assertIn("min left", app.progress_line({"current_name": "a", "current_step": 5, "total_steps": 25}, 300))


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ScriptReader:
    """Reader stand-in: white/black readings from a script, time advances 10 s a read."""

    def __init__(self, clock, white, black):
        self.clock, self.white, self.black = clock, list(white), black
        self.log = lambda _m: None

    def read(self, code, settle=0.0, samples=1):
        self.clock.now += 10
        if code == 0:
            return (0, self.black, 0)
        Y = self.white.pop(0) if self.white else self.white_default
        return (Y * 0.95, Y, Y * 1.09)

    white_default = 0.02


class Edges(unittest.TestCase):
    def test_room_light_is_explained_once_then_times_out_with_advice(self):
        clock = FakeClock()
        reader = ScriptReader(clock, [50, 50, 50, 50], black=48)      # meter on the desk under a lamp
        lines = []
        with self.assertRaisesRegex(SystemExit, "plugged in"):
            wait_for_meter(reader, lines.append, timeout=300, clock=clock)
        self.assertEqual(sum("does not come from the patch" in line for line in lines), 1)
        self.assertTrue(any("Still waiting" in line for line in lines))

    def test_meter_found(self):
        clock = FakeClock()
        white = wait_for_meter(ScriptReader(clock, [140, 141, 140, 140], black=0.0), lambda _m: None, clock=clock)
        self.assertAlmostEqual(white["Y"], 140)

    def test_a_stale_read_never_answers_a_newer_request(self):
        class SlowMeter:
            def __init__(self):
                self.calls = 0

            def read(self):
                self.calls += 1
                if self.calls == 1:
                    time.sleep(0.3)       # the first read is slow
                return (95.0, 100.0, 109.0) if self.calls == 1 else (9.5, 10.0, 10.9)

        class Pattern:
            def show(self, *args):
                pass

        service = MeterService(Pattern(), SlowMeter(), lambda _m: None)
        base = {"patch_r": 128, "patch_g": 128, "patch_b": 128, "delay_ms": 0}
        service.start_read({**base, "request_id": "first"})
        time.sleep(0.05)
        service.start_read({**base, "request_id": "second"})
        for _ in range(200):
            result = service.result()
            if result["status"] != "measuring":
                break
            time.sleep(0.01)
        self.assertEqual((result["status"], result["request_id"]), ("ok", "second"))
        self.assertAlmostEqual(result["readings"][0]["Y"], 10.0)
        time.sleep(0.4)
        self.assertEqual(service.result()["request_id"], "second")

    def test_choice_is_asked_again_not_defaulted(self):
        answers = iter(["", "7", "two", "2"])
        index, _ = quiet(app.choose_number, "Pick: ", 3, lambda _p: next(answers))
        self.assertEqual(index, 1)

    def test_closed_console_is_a_clear_stop(self):
        def closed(_prompt):
            raise EOFError
        with self.assertRaisesRegex(SystemExit, "No keyboard input"):
            app.ask("PIN: ", closed)


class Errors(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_damaged_pairing_file_is_kept_and_logged(self):
        lines = []
        lg = LG("perl", Path("helper"), self.dir, lines.append)
        (self.dir / "clients.json").write_text("{not json")
        self.assertEqual(lg.load_clients(), {})
        self.assertTrue((self.dir / "clients.json.damaged").exists())
        self.assertTrue(any("unreadable" in line for line in lines))

    def test_failed_worker_shows_its_last_log_lines(self):
        (self.dir / "worker.log").write_text("line 1\n\nDied: LG TV did not answer CAL_START\n")
        ok, out = quiet(app.report, {"status": "error", "message": "LG helper failed"}, self.dir / "worker.log")
        self.assertFalse(ok)
        self.assertIn("CAL_START", out)
        ok, out = quiet(app.report, {}, self.dir / "missing.log")
        self.assertIn("without a status", out)

    def test_failed_display_restore_tells_the_user_what_to_undo(self):
        lines = []
        display = DisplaySetup(self.dir, lambda _m: None, say=lines.append)
        display.state = {"topology_changed": True, "hdr_changed": True}
        (self.dir / "display_state.json").write_text("{}")
        with mock.patch.dict(os.environ, {"LGCAL_POWERSHELL": shutil.which("false") or "false"}):
            display.restore()
        self.assertIn("Extend", lines[0])
        self.assertIn("HDR", lines[0])

    def test_failed_download_leaves_nothing_behind(self):
        class Broken(io.BytesIO):
            headers = {"Content-Length": "100"}

            def read(self, *args):
                raise ConnectionResetError("reset by peer")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        target = self.dir / "tool.zip"
        with mock.patch("urllib.request.urlopen", return_value=Broken()):
            with self.assertRaisesRegex(SystemExit, "internet connection"):
                setup.download("https://example/tool.zip", target, lambda _m: None)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_explicit_perl_that_does_not_work_is_reported_not_replaced(self):
        with self.assertRaisesRegex(SystemExit, "settings.json"):
            setup.find_perl({"perl": str(self.dir / "perl.exe")}, lambda _m: None)


class Cleanup(unittest.TestCase):
    def test_early_failure_still_closes_the_session(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        with mock.patch.object(app, "find_perl", side_effect=SystemExit("Could not reach strawberryperl.com")):
            code, out = quiet(app.run, dict(app.DEFAULTS), session_dir=directory)
        self.assertEqual(code, 2)
        self.assertEqual(app.CONSOLE, [])
        self.assertIn("Nothing on the TV was changed", out)
        self.assertIn("strawberryperl", (directory / "console.txt").read_text())

    def test_unexpected_error_is_logged_with_traceback(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        with mock.patch.object(app, "find_perl", side_effect=KeyError("boom")):
            code, out = quiet(app.run, dict(app.DEFAULTS), session_dir=directory)
        self.assertEqual(code, 1)
        self.assertIn("unexpected error", out)
        self.assertIn("Traceback", (directory / "autocal.log").read_text())
        self.assertEqual(app.CONSOLE, [])

    def test_interrupted_pairing_does_not_leave_the_helper_running(self):
        class Process:
            def __init__(self):
                self.killed = False

            def poll(self):
                return 0 if self.killed else None

            def wait(self, timeout=None):
                if not self.killed:
                    raise app.subprocess.TimeoutExpired("helper", timeout)

            def kill(self):
                self.killed = True

        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        state, pin = directory / "state.json", directory / "pin.txt"
        state.write_text(json.dumps({"status": "pending"}))
        process = Process()

        class FakeLG:
            def connect(self, ip):
                return {"status": "error", "needs_pin_pairing": True}

            def start_pin_pairing(self, ip, session_dir):
                return process, state, pin

        def interrupt(_prompt):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            quiet(app.ensure_paired, FakeLG(), "192.0.2.1", interrupt)
        self.assertTrue(process.killed)


if __name__ == "__main__":
    unittest.main()

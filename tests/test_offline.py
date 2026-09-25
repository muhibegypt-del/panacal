"""Hardware-free checks. Run with: python -m unittest discover -s tests -t . -v"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from lgcal import app
from lgcal.display import DisplaySetup, powershell
from lgcal.lg import LG, helper_timeout
from lgcal.meter import xyz_record
from lgcal.patterns import to_8bit, window_area
from lgcal.server import Api, MeterService
from lgcal.setup import ccss_score
from lgcal.signal import delta_e_itp
from lgcal.steps import build_config, build_steps, code_for_slot

HERE = Path(__file__).resolve().parent

from tests.sim.run_sim import find_test_perl

PERL = find_test_perl()


def perl_has(module: str) -> bool:
    return bool(PERL) and subprocess.run([PERL, f"-M{module}", "-e", "1"], capture_output=True).returncode == 0


def port_free(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


class StepsTests(unittest.TestCase):
    def test_codes_match_the_dashboard_8bit_full_ladder(self):
        self.assertEqual(code_for_slot(2.3), 6)      # round(5.865)
        self.assertEqual(code_for_slot(10), 26)      # 25.5 rounds half up
        self.assertEqual(code_for_slot(50), 128)
        self.assertEqual(code_for_slot(95), 242)

    def test_order_is_white_black_then_worker_pass(self):
        steps = build_steps("2.2")
        self.assertEqual([s["ire"] for s in steps][:5], [100, 0, 50, 25, 75])
        self.assertEqual(len(steps), 25)
        white, black = steps[0], steps[1]
        self.assertTrue(white["autocal_white_reference"])
        self.assertEqual((white["r"], white["read_delay_ms"]), (255, 3000))
        self.assertTrue(black["autocal_read_only"])
        self.assertFalse(black["autocal_slot_locked"])

    def test_config_is_sdr_full_range_8bit(self):
        config = build_config({"picture_mode": "expert1", "target_gamma": "2.2"}, 142.0)
        self.assertEqual(config["max_bpc"], 8)
        self.assertEqual((config["signal_range"], config["pattern_signal_range"]), ("2", "2"))
        self.assertTrue(config["lg_autocal_sdr_1d_dpg_mode"])
        self.assertEqual(config["target_luminance"], 142.0)

    def test_limited_range_uses_legal_codes(self):
        self.assertEqual((code_for_slot(0, True), code_for_slot(50, True), code_for_slot(100, True)),
                         (16, 126, 235))
        config = build_config({}, 140.0, limited=True, picture_mode="expert2")
        self.assertEqual((config["signal_range"], config["picture_mode"]), ("1", "expert2"))
        steps = config["steps"]
        self.assertEqual((steps[0]["r"], steps[1]["r"]), (235, 16))
        self.assertAlmostEqual(steps[1]["stimulus"], 0)

    def test_bad_settings_are_rejected(self):
        with self.assertRaises(ValueError):
            build_config({"target_gamma": "2.6"})
        with self.assertRaises(ValueError):
            build_config({"picture_mode": "ISF Bright"})


class PictureModeTests(unittest.TestCase):
    class LGStub:
        def __init__(self, mode):
            self.mode = mode

        def current_picture_mode(self):
            return self.mode

    def test_reported_mode_is_used(self):
        self.assertEqual(app.choose_picture_mode(self.LGStub("filmMaker"), {}, ask=lambda _p: self.fail()),
                         "filmMaker")

    def test_unreported_mode_is_asked_not_guessed(self):
        answers = iter(["x", "0", "3"])
        self.assertEqual(app.choose_picture_mode(self.LGStub(""), {}, ask=lambda _p: next(answers)), "cinema")


class MetricTests(unittest.TestCase):
    @unittest.skipUnless(PERL, "needs perl")
    def test_delta_e_itp_matches_the_worker(self):
        for a, b in (((95.047, 100, 108.883), (96, 100, 107)), ((0.03, 0.031, 0.035), (0.029, 0.03, 0.036))):
            perl = subprocess.run([PERL, "-I", str(HERE.parent / "pgen" / "share" / "PGenerator"),
                                   "-MPGMath=delta_e_itp_xyz", "-e", "print delta_e_itp_xyz(@ARGV)",
                                   *map(str, a + b)], capture_output=True, text=True).stdout
            self.assertAlmostEqual(delta_e_itp(a, b), float(perl), places=8)

    def test_only_woled_corrections_are_picked(self):
        directory = Path(tempfile.mkdtemp())
        try:
            (directory / "WOLED_LG_C1.ccss").write_text('DISPLAY "LG OLED"\n')
            (directory / "OLEDFamily_20Jul12.ccss").write_text('TECHNOLOGY "AMOLED"\n')
            (directory / "x.ccss").write_text('DISPLAY "LG OLED C2"\nTECHNOLOGY "WRGB OLED"\n')
            self.assertEqual(ccss_score(directory / "WOLED_LG_C1.ccss"), 3)
            self.assertEqual(ccss_score(directory / "OLEDFamily_20Jul12.ccss"), 0)
            self.assertEqual(ccss_score(directory / "x.ccss"), 3)
        finally:
            shutil.rmtree(directory)


class Pattern:
    def __init__(self):
        self.shown = []

    def show(self, r, g, b, area):
        self.shown.append((r, g, b, area))


class Meter:
    def __init__(self):
        self.reads = 0

    def read(self):
        self.reads += 1
        return (95.047, 100.0, 108.883)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.pattern, self.meter = Pattern(), Meter()
        self.service = MeterService(self.pattern, self.meter, lambda _m: None)
        self.dir = Path(tempfile.mkdtemp())
        self.api = Api(self.service, LG("perl", Path("helper"), self.dir, lambda _m: None), lambda _m: None)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def wait(self):
        for _ in range(200):
            result = self.api.handle("GET", "/api/meter/read/result", {})
            if result["status"] != "measuring":
                return result
            time.sleep(0.01)
        self.fail("reading did not finish")

    def test_scaling_and_window(self):
        self.assertEqual(to_8bit(1023, 1023), 255)
        self.assertEqual(to_8bit(512, 1023), 128)
        self.assertEqual(window_area(10), 0.10)
        self.assertEqual(window_area(118), 0.10)       # APL sizes draw a 10% window

    def test_read_draws_the_patch_and_reports_the_request(self):
        start = self.api.handle("POST", "/api/meter/read", {
            "patch_r": 128, "patch_g": 128, "patch_b": 128, "input_max": 255, "patch_size": 10,
            "delay_ms": 0, "request_id": "abc", "name": "50%", "ire": 50})
        self.assertNotEqual(start["status"], "error")
        result = self.wait()
        self.assertEqual((result["status"], result["request_id"]), ("ok", "abc"))
        reading = result["readings"][0]
        self.assertAlmostEqual(reading["x"], 0.3127, places=4)
        self.assertGreaterEqual(reading["timestamp"], int(time.time()) - 2)
        self.assertEqual(self.pattern.shown[-1], (128, 128, 128, 0.10))

    def test_black_is_synthetic_on_oled(self):
        # By IRE, as meter_session.sh does, so limited black (code 16) counts.
        self.api.handle("POST", "/api/meter/read", {"patch_r": 16, "patch_g": 16, "patch_b": 16, "ire": 0,
                                                    "delay_ms": 0, "request_id": "k"})
        reading = self.wait()["readings"][0]
        self.assertEqual((reading["Y"], self.meter.reads), (0, 0))
        self.assertTrue(reading["synthetic_black"])

    def test_averaging_and_invalid_sample_count(self):
        self.api.handle("POST", "/api/meter/read", {"patch_r": 9, "patch_g": 9, "patch_b": 9, "delay_ms": 0,
                                                    "low_light": {"requested_sample_count": 3}})
        self.assertEqual(self.wait()["readings"][0]["sample_count"], 3)
        self.api.handle("POST", "/api/meter/read", {"patch_r": 9, "patch_g": 9, "patch_b": 9, "delay_ms": 0,
                                                    "low_light": {"requested_sample_count": 4}})
        self.assertEqual(self.wait()["status"], "error")

    def test_stop_pattern_and_unknown_routes(self):
        self.assertEqual(self.api.handle("POST", "/api/pattern", {"name": "stop"})["status"], "ok")
        self.assertEqual(self.pattern.shown[-1], (0, 0, 0, 1.0))
        self.assertEqual(self.api.handle("POST", "/api/lg/hdr-tone-map/upload", {})["status"], "error")

    def test_cct(self):
        self.assertAlmostEqual(xyz_record((95.047, 100, 108.883))["cct"], 6504, delta=2)


class LGRouteTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.lg = LG("perl", Path("helper"), self.dir, lambda _m: None)
        self.requests = []
        self.lg.run_helper = lambda request: (self.requests.append(request) or
                                              {"status": "ok", "ddc_1d_lut": True, "client_key": "k"})

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_routes_need_a_paired_tv(self):
        self.assertEqual(self.lg.picture_settings({})["status"], "error")
        self.assertEqual(self.requests, [])

    def test_ddc_write_holds_calibration_mode_like_lg_pm(self):
        self.lg.save_clients({"ip": "192.0.2.1", "client_key": "k"})
        arrays = {k: [0] * 26 for k in ("whiteBalanceRed", "whiteBalanceGreen", "whiteBalanceBlue")}
        self.lg.picture_settings_set({"settings": {"whiteBalanceMethod": "22", **arrays},
                                      "picture_mode": "expert1"})
        request = self.requests[-1]
        self.assertEqual((request["keep_calibration_mode"], request["calibration_mode_active"]), (1, 0))
        clients = self.lg.load_clients()
        self.assertTrue(clients["calibration_mode"])
        self.assertEqual(clients["calibration_picture_mode"], "expert1")

    def test_dpg_upload_validates_and_clamps(self):
        self.lg.save_clients({"ip": "192.0.2.1", "client_key": "k"})
        self.assertEqual(self.lg.dpg_upload({"dpg_data": [1] * 10})["received_count"], 10)
        self.lg.dpg_upload({"dpg_data": [-5] + [70000] * 3071, "keep_calibration_mode": True})
        data = self.requests[-1]["dpg_data"]
        self.assertEqual((data[0], data[1]), (0, 65535))

    def test_stale_calibration_mode_is_closed(self):
        self.lg.save_clients({"ip": "192.0.2.1", "client_key": "k", "calibration_mode": True,
                              "calibration_picture_mode": "expert2"})
        self.lg.clear_stale_calibration_mode()
        self.assertEqual((self.requests[-1]["action"], self.requests[-1]["enable"]), ("calibration_mode", 0))
        self.assertFalse(self.lg.load_clients()["calibration_mode"])
        self.requests.clear()
        self.assertIsNone(self.lg.clear_stale_calibration_mode())
        self.assertEqual(self.requests, [])

    def test_timeouts_match_lg_pm(self):
        self.assertEqual(helper_timeout({"action": "picture_set", "settings": {"whiteBalanceRed": []}}), 150)
        self.assertEqual(helper_timeout({"action": "1d_dpg_upload"}), 80)
        self.assertEqual(helper_timeout({"action": "picture_get", "helper_timeout": 90}), 90)


@unittest.skipUnless(perl_has("IO::Socket::SSL") and shutil.which("openssl") and port_free(3001) and port_free(3000),
                     "needs perl IO::Socket::SSL, openssl and free ports 3000/3001")
class TransportTests(unittest.TestCase):
    """The real pgenerator-lg helper over wss://3001 with the PC-PORT TLS patch."""

    def setUp(self):
        from tests.fake_webos import FakeWebOS
        self.dir = Path(tempfile.mkdtemp())
        self.tv = FakeWebOS(self.dir)
        self.saved = app.DATA_DIR
        app.DATA_DIR = self.dir / "data"

    def tearDown(self):
        app.DATA_DIR = self.saved
        self.tv.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_find_pair_then_stored_key(self):
        from tests.fake_webos import KEY, PIN
        lg = LG(PERL, app.HELPER, app.DATA_DIR, lambda _m: None, app.perl_env(None, None))
        settings = {"tv_ip": "127.0.0.1"}
        self.assertEqual(app.find_tv(lg, settings, pin_input=lambda _p: PIN), "127.0.0.1")
        self.assertEqual(json.loads((app.DATA_DIR / "clients.json").read_text())["client_key"], KEY)
        self.assertIn("ssap://pairing/setPin", self.tv.log)
        app.find_tv(lg, settings, pin_input=lambda _p: self.fail("asked for a PIN again"))


def find_powershell() -> str:
    candidate = powershell()
    return candidate if shutil.which(candidate) or Path(candidate).is_file() else ""


@unittest.skipUnless(find_powershell(), "needs PowerShell")
class DisplayScriptTests(unittest.TestCase):
    """display_setup.ps1 against a fake DisplayTool in the Panasonic-run state:
    TV duplicated with the monitor and HDR on."""

    def test_extend_find_lg_hdr_off_then_restore(self):
        import os
        work = Path(tempfile.mkdtemp())
        try:
            shutil.copy(HERE.parent / "lgcal" / "display_setup.ps1", work)
            shutil.copy(HERE / "fake_display" / "DisplayTool.cs", work)
            calls = work / "calls.log"
            os.environ["FAKE_DISPLAY_LOG"] = str(calls)
            setup = DisplaySetup(work, lambda _m: None, work)
            state = setup.prepare()
            self.assertEqual((state["device"], state["name"]), ("\\\\.\\DISPLAY2", "LG TV SSCR2"))
            self.assertTrue(state["topology_changed"] and state["hdr_changed"])
            setup.restore()
            self.assertEqual(calls.read_text().split("\n")[:4],
                             ["topology 4", "hdr 77 2 False", "hdr 77 2 True", "topology 2"])
        finally:
            os.environ.pop("FAKE_DISPLAY_LOG", None)
            shutil.rmtree(work, ignore_errors=True)


@unittest.skipUnless(PERL, "needs perl")
class SimulationTests(unittest.TestCase):
    """The complete worker run against a simulated LG OLED and meter."""

    def simulate(self, **kwargs):
        from tests.sim.run_sim import simulate
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        code, state, tv, meter, console = simulate(directory, **kwargs)
        verification = directory / "verification.json"
        return code, state, tv, console, json.loads(verification.read_text()) if verification.exists() else {}

    def assert_calibrated(self, code, state, tv, console, verification):
        self.assertEqual((code, state.get("status")), (0, "complete"), console[-2000:])
        self.assertTrue(state["sdr_1d_dpg_single_socket_commit"])
        self.assertFalse(tv.calibration_mode, "calibration mode must be closed at the end")
        # Independent measurement of the committed calibration (meter noise
        # 0.3% luminance): D65 and the target curve at every level.
        self.assertLess(verification["average_de"], 0.6)
        self.assertLess(verification["max_de"], 1.2)
        for row in verification["rows"]:
            self.assertAlmostEqual(row["x"], 0.3127, delta=0.0015)
            self.assertAlmostEqual(row["y"], 0.3290, delta=0.0015)

    def test_full_range_tv(self):
        result = self.simulate(tv_mode="expert2")
        self.assert_calibrated(*result)
        code, state, tv, console, _ = result
        self.assertIn("full-range", console)
        self.assertIn("Expert (Dark Room)", console)
        # The wizard's preparation, in its order, before the worker starts.
        prep = [a for a in tv.requests if a in ("picture_reset", "picture_set", "sdr_calman_reset",
                                                  "1d_dpg_upload")]
        self.assertEqual(prep[:3], ["picture_reset", "picture_set", "sdr_calman_reset"])
        # The reset put OLED brightness to factory; the user's value is back.
        self.assertEqual(tv.backlight, tv.USER_BACKLIGHT)

    def test_leftover_calibration_is_cleared_first(self):
        from tests.sim.sim_tv import Panel, SimTV, identity
        from lgcal.prepare import prepare
        tv = SimTV(Panel())
        self.assertNotAlmostEqual(tv.panel.dpg[2][500], identity(500))

        class Direct:  # the LG routes, answered straight by the simulated TV
            def picture_settings(self, payload):
                return tv.handle({"action": "picture_get", **payload})

            def picture_reset(self, payload):
                return tv.handle({"action": "picture_reset", **payload})

            def picture_settings_set(self, payload):
                return tv.handle({"action": "picture_set", **payload})

            def sdr_calman_reset(self, payload):
                return tv.handle({"action": "sdr_calman_reset", **payload})

        prepare(Direct(), "expert2", "Expert (Dark Room)", lambda _m: None, sleep=lambda _s: None)
        self.assertAlmostEqual(tv.panel.dpg[2][500], identity(500))
        self.assertEqual(tv.backlight, tv.USER_BACKLIGHT)

    def test_tv_on_black_level_low_gets_limited_patterns(self):
        result = self.simulate(black_level="low")
        self.assert_calibrated(*result)
        self.assertIn("limited-range", result[3])

    def test_lifted_black_stops_before_touching_the_tv(self):
        code, state, tv, console, _ = self.simulate(gpu_range="limited")
        self.assertEqual(code, 2)
        self.assertIn("Black is lifted", console)
        self.assertEqual(tv.uploads, 0)

    def test_stale_session_is_closed_first(self):
        code, state, tv, console, verification = self.simulate(stale_calibration=True, tv_mode="filmMaker")
        self.assertEqual(tv.requests[0], "calibration_mode")
        self.assertIn("Closed a calibration session", console)
        self.assertEqual(state["picture_mode"], "filmMaker")


if __name__ == "__main__":
    unittest.main()

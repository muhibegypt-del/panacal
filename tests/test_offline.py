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
from lgcal.lg import LG, helper_timeout
from lgcal.meter import xyz_record
from lgcal.patterns import to_8bit, window_area
from lgcal.server import Api, MeterService
from lgcal.steps import build_config, build_steps, code_for_slot

PERL = shutil.which("perl")


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

    def test_bad_settings_are_rejected(self):
        with self.assertRaises(ValueError):
            build_config({"target_gamma": "2.6"})
        with self.assertRaises(ValueError):
            build_config({"picture_mode": "ISF Bright"})


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
        self.api.handle("POST", "/api/meter/read", {"patch_r": 0, "patch_g": 0, "patch_b": 0,
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

    def test_pin_pairing_then_stored_key(self):
        from tests.fake_webos import KEY, PIN
        settings = {"tv_ip": "127.0.0.1", "perl": PERL}
        self.assertEqual(app.pair(settings, pin_input=lambda _p: PIN), 0)
        self.assertEqual(json.loads((app.DATA_DIR / "clients.json").read_text())["client_key"], KEY)
        self.assertIn("ssap://pairing/setPin", self.tv.log)
        self.assertEqual(app.pair(settings, pin_input=lambda _p: self.fail("asked for a PIN again")), 0)


@unittest.skipUnless(PERL, "needs perl")
class SimulationTests(unittest.TestCase):
    """The complete worker run against a simulated LG OLED and meter."""

    def test_full_sdr_greyscale_calibration(self):
        from tests.sim.run_sim import simulate
        directory = Path(tempfile.mkdtemp())
        saved = app.DATA_DIR
        try:
            code, state, tv, meter = simulate(directory)
        finally:
            app.DATA_DIR = saved
        self.assertEqual((code, state["status"]), (0, "complete"), state.get("message"))
        # The worker targets dE ITP 0.5 against a meter with 0.3% noise;
        # it may stop a level at its iteration budget just above that.
        self.assertLess(state["sdr_1d_dpg_final_de"], 1.0)
        best = sorted(min(float(e["de"]) for e in entries)
                      for entries in state["sdr_1d_dpg_anchor_history"].values())
        self.assertLess(best[len(best) // 2], 0.5, "median level must reach the target")
        self.assertTrue(state["sdr_1d_dpg_single_socket_commit"])
        self.assertFalse(tv.calibration_mode, "calibration mode must be closed at the end")
        self.assertGreater(tv.uploads, 20)
        shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

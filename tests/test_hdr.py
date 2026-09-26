"""HDR10: the request bodies, madTPG control, the HDR wait and the full
run of the author's HDR workers against a simulated HDR LG."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lgcal import app
from lgcal.hdr import (HDR_CODES_10BIT_FULL, HDR_PICTURE_MODES, HDR_SLOTS, build_hdr_colour_config,
                       build_hdr_greyscale_config, colour_patches, hdr_dark_threshold, hdr_index, hdr_steps,
                       pq_decode, pq_encode)
from lgcal import madtpg
from lgcal.madtpg import HDR_METADATA, MadTPGPatterns, find_madvr, wait_for_hdr


class Bodies(unittest.TestCase):
    def test_greyscale_body_is_the_dashboards_hdr10_request(self):
        config = build_hdr_greyscale_config({}, 700.0, "hdrCinema", 0.3)
        self.assertEqual((config["signal_mode"], config["max_bpc"], config["pattern_signal_range"]),
                         ("hdr10", 10, "2"))
        self.assertTrue(config["lg_autocal_hdr20_dpg_mode"])
        self.assertTrue(config["full_workflow"])          # held for the colour stage
        self.assertNotIn("lg_autocal_sdr_1d_dpg_mode", config)
        self.assertNotIn("headroom_target_luminance", config)
        steps = config["steps"]
        self.assertEqual(len(steps), 21)
        self.assertTrue(steps[0]["autocal_read_only"])
        self.assertEqual([s["r"] for s in steps[1:]], list(reversed(HDR_CODES_10BIT_FULL)))
        self.assertEqual({s["ddc_layout"] for s in steps[1:]}, {"hdr20"})
        self.assertEqual([s["ire"] for s in steps[1:]], list(reversed(HDR_SLOTS)))

    def test_dark_levels_follow_the_meter_floor_on_the_2_2_curve(self):
        self.assertEqual(hdr_dark_threshold(700.0, 0.3), 4.0)   # 4% = 0.58 cd/m2, 2.7% = 0.25
        self.assertEqual(hdr_dark_threshold(300.0, 0.3), 5.0)
        config = build_hdr_greyscale_config({}, 700.0, "hdrCinema", 0.3)
        self.assertEqual((config["lg_autocal_hdr20_dpg_low_ire_threshold"],
                          config["lg_autocal_hdr20_dpg_inner_iters_low"],
                          config["lg_autocal_hdr20_dpg_inner_iters_very_low"]), (4.0, 1, 1))
        self.assertNotIn("lg_autocal_hdr20_dpg_low_ire_threshold",
                         build_hdr_greyscale_config({}, 700.0, "hdrCinema", 0.0))

    def test_indexes_match_the_worker_table(self):
        self.assertEqual([hdr_index(s) for s in (1.4, 4, 10, 50, 100)], [14, 42, 103, 514, 1023])
        self.assertEqual(hdr_index(3), 31)

    def test_colour_body_inherits_the_session_and_carries_peak_and_greyscale(self):
        config = build_hdr_colour_config({"method": "matrix"}, 663.0, [5] * 3072)
        self.assertEqual((config["target_gamut"], config["target_gamma"], config["upload_command"]),
                         ("bt2020", "st2084", "BT2020_3D_LUT_DATA"))
        self.assertEqual((config["full_workflow_peak_luminance"], len(config["full_workflow_dpg_data"])),
                         (663.0, 3072))
        self.assertTrue(config["skip_preprofile_unity_reset"])
        self.assertNotIn("full_workflow_dpg_data", build_hdr_colour_config({}, 663.0, None))

    def test_pq(self):
        self.assertAlmostEqual(pq_decode(0.5), 92.25, places=1)
        self.assertAlmostEqual(pq_decode(pq_encode(100.0)), 100.0, places=6)
        self.assertEqual((pq_decode(0), pq_encode(0)), (0.0, 0.0))
        red = colour_patches()[0]
        self.assertAlmostEqual(red[2][1], 21.267, delta=0.01)       # BT.709 red on a 100 cd/m2 white
        self.assertTrue(all(0 <= v <= 1 for _n, signal, _t in colour_patches() for v in signal))


class FakeMadHcNet:
    def __init__(self, connects=True, shows=True):
        self.calls, self.connects, self.shows, self.refuse = [], connects, shows, set()

    def connect(self, timeout_ms):
        self.calls.append(("connect", timeout_ms))
        return self.connects

    def levels(self):
        return (16, 235)

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, *args))
            return self.shows if name == "ShowRGB" else name not in self.refuse
        return call


class MadTPG(unittest.TestCase):
    def test_codes_go_through_at_full_precision_and_area_is_set_once(self):
        api = FakeMadHcNet()
        tpg = MadTPGPatterns(Path("."), lambda _m: None, api=api, launch=False)
        tpg.show_code(512, 512, 512, 1023, 0.10)
        tpg.show(255, 0, 0, 0.10)
        shows = [c for c in api.calls if c[0] == "ShowRGB"]
        self.assertAlmostEqual(shows[0][1], 512 / 1023)
        self.assertEqual(shows[1][1:], (1.0, 0.0, 0.0))
        self.assertEqual([c for c in api.calls if c[0] == "SetPatternConfig"], [("SetPatternConfig", 10, 0, 0, 0)])
        self.assertIn(("Disable3dlut",), api.calls)
        tpg.close()
        self.assertIn(("Disconnect",), api.calls)

    def test_failures_are_reported(self):
        with self.assertRaises(SystemExit):
            MadTPGPatterns(Path("."), lambda _m: None, api=FakeMadHcNet(connects=False), launch=False)
        tpg = MadTPGPatterns(Path("."), lambda _m: None, api=FakeMadHcNet(shows=False), launch=False)
        with self.assertRaises(RuntimeError):
            tpg.show(1, 1, 1, 0.1)

    def test_window_goes_fullscreen_on_the_tv_and_hdr_is_pressed(self):
        api = FakeMadHcNet()
        tpg = MadTPGPatterns(Path("."), lambda _m: None, api=api, launch=False, sleep=lambda _s: None)
        self.assertTrue(tpg.to_screen({"device": r"\\.\DISPLAY2", "x": 1920, "y": 0, "width": 3840,
                                       "height": 2160}))
        self.assertIn(("place", 1920 + 960, 540, 1920 + 2880, 1620), api.calls)
        self.assertLess(api.calls.index(("place", 2880, 540, 4800, 1620)), api.calls.index(("EnterFullscreen",)))
        self.assertTrue(tpg.hdr_on())
        self.assertEqual(api.calls[-3:], [("SetHdrMetadata", *HDR_METADATA), ("SetHdrButton", True),
                                          ("IsHdrButtonPressed",)])
        self.assertFalse(tpg.to_screen({"device": ""}))          # display setup failed: user drags it

    def test_refusals_fall_back_to_asking(self):
        api = FakeMadHcNet()
        api.refuse = {"IsFullscreen", "IsHdrButtonPressed"}
        tpg = MadTPGPatterns(Path("."), lambda _m: None, api=api, launch=False, sleep=lambda _s: None)
        self.assertFalse(tpg.to_screen({"x": 0, "y": 0, "width": 1920, "height": 1080}))
        self.assertFalse(tpg.hdr_on())

    def test_finds_madtpg_inside_the_zips_folder(self):
        tools = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tools, True)
        inner = tools / "madVR" / "madVR"
        inner.mkdir(parents=True)
        for name in ("madTPG.exe", "madHcNet64.dll"):
            (inner / name).write_bytes(b"")
        saved = madtpg.TOOLS, madtpg.registered_madvr
        madtpg.TOOLS, madtpg.registered_madvr = tools, lambda: None
        self.addCleanup(lambda: setattr(madtpg, "TOOLS", saved[0]) or setattr(madtpg, "registered_madvr", saved[1]))
        self.assertEqual(find_madvr({}, self.fail), inner)      # no second download

    def test_wait_for_hdr_only_asks_what_madtpg_could_not_do(self):
        class LG:
            def current_picture_mode(self):
                return "hdrCinema"
        said = []
        self.assertEqual(wait_for_hdr(LG(), said.append, HDR_PICTURE_MODES, on_screen=True, hdr=True), "hdrCinema")
        self.assertEqual(said, [])

        class Later:
            modes = ["expert2", "hdrFilmMaker"]

            def current_picture_mode(self):
                return self.modes.pop(0)
        wait_for_hdr(Later(), said.append, HDR_PICTURE_MODES, on_screen=True, hdr=True, sleep=lambda _s: None)
        text = "\n".join(said)
        self.assertIn("sending HDR10", text)
        self.assertNotIn("Drag", text)
        self.assertNotIn("'HDR' button", text)
        self.assertIn("Dynamic Tone Mapping", text)

    def test_wait_for_hdr(self):
        class LG:
            def __init__(self, modes):
                self.modes = list(modes)

            def current_picture_mode(self):
                return self.modes.pop(0) if len(self.modes) > 1 else self.modes[0]
        said = []
        tick = iter(range(0, 10000, 2))
        self.assertEqual(wait_for_hdr(LG(["expert2", "hdrStandard", "hdrCinema"]), said.append, HDR_PICTURE_MODES,
                                      clock=lambda: next(tick), sleep=lambda _s: None), "hdrCinema")
        self.assertTrue(any("hdrStandard" in line for line in said))
        tick = iter(range(0, 10000, 100))
        with self.assertRaises(SystemExit):
            wait_for_hdr(LG(["expert2"]), said.append, HDR_PICTURE_MODES, clock=lambda: next(tick),
                         sleep=lambda _s: None)


class HdrRun(unittest.TestCase):
    def test_the_authors_hdr_workers_calibrate_a_simulated_hdr_lg(self):
        from tests.sim.run_sim import simulate
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        code, _state, tv, _meter, console = simulate(directory, peak=700.0, hdr=True)
        self.assertEqual(code, 0, console[-3000:])
        self.assertEqual((tv.tone_maps, tv.lut_uploads), (1, 1))
        self.assertIn("hdr_calman_reset", tv.requests)
        self.assertFalse(tv.calibration_mode, "the tone map must end calibration mode")
        self.assertIn("HDR levels below 4%", console)
        self.assertTrue((directory / "dark_end.json").exists())
        result = json.loads((directory / "verification.json").read_text())
        self.assertLess(result["average_de"], 0.6)
        self.assertLess(result["max_de"], 1.2)
        for row in result["rows"]:
            if not row["below_floor"] and not row["tone_mapped"]:
                self.assertAlmostEqual(row["Y"] / row["target_Y"], 1.0, delta=0.03, msg=f"{row['signal']}%")
        self.assertLess(result["colour_average_de"], 1.5)
        self.assertLess(result["colour_max_de"], 3.0)


if __name__ == "__main__":
    unittest.main()

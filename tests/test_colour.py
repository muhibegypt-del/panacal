"""The colour stage: the author's 3D LUT worker against a TV whose factory
colour conversion was cleared, the greyscale it must keep, and the pieces
it is built from."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lgcal import app
from lgcal.colour import (build_colour_config, colour_report, colour_target, commit_greyscale,
                          find_saved_greyscale)
from tests.sim.sim_tv import NATIVE_PRIMARIES


class Pieces(unittest.TestCase):
    def test_config_is_an_sdr_matrix_run_that_leaves_greys_to_the_1d_lut(self):
        config = build_colour_config({"target_gamma": "bt1886", "patch_size": 10}, limited=False,
                                     picture_mode="expert2", lut_dir=Path("C:/x/luts"), run_id="r1")
        self.assertEqual((config["method"], config["target_gamut"], config["signal_mode"]),
                         ("matrix", "bt709", "sdr"))
        self.assertEqual((config["max_bpc"], config["pattern_signal_range"], config["include_greyscale"]),
                         (8, "2", 0))
        self.assertTrue(config["upload"])
        self.assertEqual(build_colour_config({}, limited=True, picture_mode="expert2", lut_dir=Path("."),
                                             run_id="r")["signal_range"], "1")
        with self.assertRaises(ValueError):
            build_colour_config({"target_gamma": "2.6"}, limited=False, picture_mode="expert2",
                                lut_dir=Path("."), run_id="r")

    def test_bt709_targets(self):
        red = colour_target(200.0, 1, 0, 0)
        self.assertAlmostEqual(red[1], 200 * 0.2126729, places=4)
        self.assertAlmostEqual(red[0] / sum(red), 0.64, places=3)
        self.assertAlmostEqual(colour_target(100.0, 1, 1, 1)[1], 100.0, places=3)

    def test_report(self):
        self.assertEqual(colour_report({"status": "complete", "upload_verified": True})[0], True)
        self.assertEqual(colour_report({"status": "complete", "upload_message": "nope"}), (False, "nope"))
        self.assertEqual(colour_report({"status": "error", "message": "meter"}), (False, "meter"))

    def test_commit_needs_both_calibration_responses(self):
        class LG:
            def __init__(self, result):
                self.result, self.payloads = result, []

            def dpg_upload(self, payload):
                self.payloads.append(payload)
                return self.result
        both = {"status": "ok", "cal_start_response": {"type": "response"}, "cal_end_response": {"type": "response"}}
        lg = LG(both)
        self.assertTrue(commit_greyscale(lg, [1] * 3072, "expert2")["committed"])
        self.assertEqual((lg.payloads[0]["keep_calibration_mode"], lg.payloads[0]["calibration_mode_active"]),
                         (False, False))
        self.assertFalse(commit_greyscale(LG({"status": "ok"}), [1] * 3072, "expert2")["committed"])
        with self.assertRaises(ValueError):
            commit_greyscale(LG(both), [1] * 10, "expert2")


class SavedGreyscale(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def session(self, name, mode="expert2", status="complete", committed=True, table=1, dark=None):
        directory = self.root / name
        directory.mkdir()
        (directory / "worker_config.json").write_text(json.dumps({"picture_mode": mode}))
        (directory / "worker_state.json").write_text(json.dumps({
            "status": status, "sdr_1d_dpg_single_socket_commit": committed, "sdr_1d_dpg_data": [table] * 3072}))
        if dark is not None:
            (directory / "dark_end.json").write_text(json.dumps({"committed_table": [dark] * 3072}))
        return directory

    def test_latest_finished_run_of_the_mode_and_what_the_tv_received(self):
        self.session("20260925_220000", table=1)
        self.session("20260926_000100", table=2, dark=3)             # the dark end is what was committed
        self.session("20260926_010000", mode="game", table=4)        # another mode
        self.session("20260926_020000", status="error", table=5)     # did not finish
        self.session("20260926_030000", committed=False, table=6)    # commit not confirmed
        (self.root / "20260926_040000_undo").mkdir()                 # not a calibration run
        table, source = find_saved_greyscale(self.root, "expert2")
        self.assertEqual((table[0], source.name), (3, "20260926_000100"))
        self.assertEqual(find_saved_greyscale(self.root, "cinema"), (None, None))
        self.assertEqual(find_saved_greyscale(self.root / "missing", "expert2"), (None, None))


class WideGamutTV(unittest.TestCase):
    """The real workers against a TV left on its native primaries."""

    def run_sim(self, root: Path, name: str, **kwargs):
        from tests.sim.run_sim import simulate
        saved = app.SESSIONS
        app.SESSIONS = root
        try:
            return simulate(root / name, peak=200.0, **kwargs)
        finally:
            app.SESSIONS = saved

    def assert_bt709(self, directory: Path):
        verification = json.loads((directory / "verification.json").read_text())
        colours = verification["colours"]
        self.assertLess(colours["average_de"], 0.8)
        self.assertLess(colours["max_de"], 1.5)
        for row in colours["rows"]:
            self.assertAlmostEqual(row["x"], row["target_x"], delta=0.003, msg=row["name"])
            self.assertAlmostEqual(row["y"], row["target_y"], delta=0.003, msg=row["name"])
        self.assertLess(verification["average_de"], 0.6)       # greys
        return verification

    def test_full_run_calibrates_greys_then_colours(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        code, state, tv, _meter, console = self.run_sim(root, "20260926_000001")
        self.assertEqual(code, 0, console[-3000:])
        self.assertEqual(tv.lut_uploads, 1)
        self.assertIn("The colour correction (3D LUT) is saved in the TV.", console)
        self.assert_bt709(root / "20260926_000001")
        # The greyscale in the TV is the one the greyscale stage committed.
        committed = json.loads((root / "20260926_000001" / "dark_end.json").read_text())["committed_table"]
        self.assertEqual([round(v) for c in tv.panel.dpg for v in c], committed)

    def test_colour_only_keeps_the_saved_greyscale(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        code, _state, tv, _meter, console = self.run_sim(root, "20260926_000001")
        self.assertEqual(code, 0, console[-3000:])
        # Tonight's TV: greyscale calibrated, colours native (no 3D LUT).
        tv.panel.lut = None
        greys = [list(c) for c in tv.panel.dpg]
        red = tv.panel.xyz((255, 0, 0))
        self.assertAlmostEqual(red[0] / sum(red), NATIVE_PRIMARIES[0][0], delta=0.002)
        resets = tv.requests.count("picture_reset")
        code, _state, tv, _meter, console = self.run_sim(root, "20260926_000002", tv=tv, stages=("colour",))
        self.assertEqual(code, 0, console[-3000:])
        self.assertIn("Greyscale to keep: the calibration from 20260926_000001", console)
        self.assertEqual(tv.requests.count("picture_reset"), resets, "the colour run must not reset the mode")
        self.assertEqual([list(c) for c in tv.panel.dpg], greys)
        self.assert_bt709(root / "20260926_000002")

    def test_colour_only_without_a_saved_greyscale_says_so(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        code, _state, tv, _meter, console = self.run_sim(root, "20260926_000003", stages=("colour",))
        self.assertIn("No finished greyscale calibration", console)
        self.assertEqual(code, 0, console[-3000:])
        self.assertEqual(tv.lut_uploads, 1)


if __name__ == "__main__":
    unittest.main()

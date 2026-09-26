"""Levels too dark for the meter: worker settings, the dark-end extension,
the final-commit hook and the verification's treatment of them."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lgcal import settings as settings_file
from lgcal.dark import extend_dark_end, fit_exponent, is_final_commit, sample_index
from lgcal.server import Api
from lgcal.signal import summarize
from lgcal.steps import build_config, code_for_slot, dark_overrides, dark_threshold, stimulus_for_code, target_yn


def power_table(exponents=(1.1, 1.0, 0.9), scale=(0.8, 0.9, 1.0)) -> list[int]:
    out = []
    for k, s in zip(exponents, scale):
        out += [int(round(s * 32767 * (i / 1023) ** k)) for i in range(1024)]
    return out


class Threshold(unittest.TestCase):
    def test_follows_the_measured_white(self):
        # G2 run: white 201 -> 7% is 0.35 cd/m2 (tracked), 5% is 0.16 (did not).
        self.assertEqual(dark_threshold(201.0, "bt1886"), 7.0)
        self.assertEqual(dark_threshold(150.0, "bt1886"), 10.0)
        self.assertEqual(dark_threshold(20.0, "bt1886"), 10.0)       # the worker caps at 10%
        self.assertEqual(dark_threshold(1000.0, "bt1886", floor=0.0), 2.3)

    def test_overrides(self):
        self.assertEqual(dark_overrides(201.0, "bt1886"),
                         {"lg_autocal_sdr26_dpg_low_ire_threshold": 7.0, "lg_autocal_sdr26_dpg_inner_iters_low": 1})
        self.assertEqual(dark_overrides(201.0, "bt1886", floor=0.0), {})

    def test_config_carries_them_and_the_setting_changes_them(self):
        settings, _ = settings_file.validate({})
        config = build_config(settings, 201.0)
        self.assertEqual(config["lg_autocal_sdr26_dpg_low_ire_threshold"], 7.0)
        settings, _ = settings_file.validate({"meter": {"floor_cd_m2": 0}})
        self.assertNotIn("lg_autocal_sdr26_dpg_low_ire_threshold", build_config(settings, 201.0))
        with self.assertRaises(ValueError):
            settings_file.validate({"meter": {"floor_cd_m2": -1}})


class Extension(unittest.TestCase):
    def test_indexes_match_the_worker_log(self):
        self.assertEqual([sample_index(s) for s in (2.3, 3, 4, 5, 7, 10, 15, 20)],
                         [22, 29, 36, 48, 66, 95, 139, 187])

    def test_power_law_is_carried_to_black(self):
        truth = power_table()
        damaged = list(truth)
        for c in range(3):
            for i in range(1, 66):
                damaged[c * 1024 + i] = 2286          # the flat, too-bright plateau from the G2 run
        extended, report = extend_dark_end(damaged, 7.0)
        self.assertEqual(report["fit_slots"], [7, 10, 15, 20])
        for c, k in enumerate((1.1, 1.0, 0.9)):
            self.assertAlmostEqual(report["exponents"][c], k, delta=0.02)
            for i in (22, 36, 48, 65):
                self.assertAlmostEqual(extended[c * 1024 + i], truth[c * 1024 + i], delta=max(3, truth[c * 1024 + i] * 0.03))
        for c in range(3):
            table = extended[c * 1024:(c + 1) * 1024]
            self.assertEqual(table[0], 0)
            self.assertEqual(table, sorted(table[:66]) + table[66:])
            self.assertEqual(table[66:], damaged[c * 1024 + 66:(c + 1) * 1024])

    def test_guards(self):
        with self.assertRaises(ValueError):
            extend_dark_end([0] * 10, 7.0)
        with self.assertRaises(ValueError):
            extend_dark_end(power_table(), 35.0)
        self.assertEqual(fit_exponent([]), 1.0)
        self.assertEqual(fit_exponent([(10, 0.0), (20, 5.0)]), 1.0)
        self.assertEqual(fit_exponent([(10, 1.0), (20, 1000.0)]), 1.4)


class FinalCommitHook(unittest.TestCase):
    class LG:
        def __init__(self):
            self.uploads = []

        def dpg_upload(self, payload):
            self.uploads.append(payload)
            return {"status": "ok"}

    def test_only_the_commit_is_rewritten(self):
        self.assertTrue(is_final_commit({"keep_calibration_mode": False, "calibration_mode_active": False}))
        self.assertFalse(is_final_commit({"keep_calibration_mode": True, "calibration_mode_active": True}))
        lg = self.LG()
        api = Api(None, lg, lambda _m: None, final_dpg=lambda table: [1] * len(table))
        api.dpg_upload({"dpg_data": [5] * 3072, "keep_calibration_mode": True, "calibration_mode_active": True})
        api.dpg_upload({"dpg_data": [5] * 3072, "keep_calibration_mode": False, "calibration_mode_active": False})
        self.assertEqual([u["dpg_data"][0] for u in lg.uploads], [5, 1])


class Verification(unittest.TestCase):
    def test_rows_below_the_floor_are_shown_not_scored(self):
        rows = [{"de": 0.2}, {"de": 0.4}, {"de": 41.0, "below_floor": True}]
        summary = summarize(rows)
        self.assertAlmostEqual(summary["average_de"], 0.3)
        self.assertEqual(summary["max_de"], 0.4)
        self.assertEqual(len(summary["rows"]), 3)
        with self.assertRaises(ValueError):
            summarize([{"de": 1.0, "below_floor": True}])


class DarkBlindMeterRun(unittest.TestCase):
    def test_dark_end_lands_on_target_with_a_meter_blind_below_025(self):
        from tests.sim.run_sim import simulate
        from tests.sim.sim_tv import DarkBlindMeter
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        code, state, tv, _meter, console = simulate(directory, peak=200.0, meter_class=DarkBlindMeter)
        self.assertEqual((code, state.get("status")), (0, "complete"), console[-2000:])
        self.assertIn("Levels below 7%", console)
        self.assertTrue((directory / "dark_end.json").exists())
        self.assertIn("not counted", console)
        white = tv.panel.xyz((255, 255, 255))[1]
        for slot in (2.3, 3, 4, 5, 7, 10):
            code8 = code_for_slot(slot)
            ratio = tv.panel.xyz((code8,) * 3)[1] / (white * target_yn(stimulus_for_code(code8), "bt1886"))
            self.assertAlmostEqual(ratio, 1.0, delta=0.1, msg=f"{slot}%")
        verification = json.loads((directory / "verification.json").read_text())
        self.assertLess(verification["max_de"], 1.2)


if __name__ == "__main__":
    unittest.main()

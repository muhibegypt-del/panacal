"""ChArUco photo mode: patterns, metrics and the analyser on synthetic photos
with a known geometry error, lens distortion, an off-axis camera, bezel
parallax, blur, bloom, grille moire, noise and JPEG."""
from __future__ import annotations

import unittest

import numpy as np

from iiyama import charuco_geo as g
from iiyama import charuco_sim as s

SHAPE = ("pincushion", "sidepin_top", "sidepin_bottom", "pinbalance",
         "pinbalance_top", "pinbalance_bottom", "vlin_side", "trapezoid")
ALL = SHAPE + ("tilt", "parallelogram", "vlin_corner", "hsize", "vsize", "hpos", "vpos")


def cam_dict(c, views=8):
    return {"K": c["K"], "dist": c["dist"], "size": c["size"], "views": views}


class Patterns(unittest.TestCase):
    def test_crt_board_fills_the_raster_and_detects_fully(self):
        img = g.crt_pattern()
        self.assertEqual(img.shape, (1200, 1600))
        self.assertEqual(set(np.unique(img)), {0, g.WHITE})
        for edge in (img[0], img[-1], img[:, 0], img[:, -1]):
            self.assertTrue((edge == g.WHITE).all())
        pts, ids = g.detect_charuco(img, g.crt_board())
        self.assertEqual(len(ids), 31 * 23)
        ideal = g.crt_board().getChessboardCorners()[ids, :2] - 0.5   # pixel-centre coords
        self.assertLess(np.abs(pts - ideal).max(), 0.1)

    def test_lcd_and_crt_boards_cannot_be_confused(self):
        _, ids = g.detect_charuco(g.lcd_pattern(), g.crt_board())
        self.assertEqual(len(ids), 0)
        _, ids = g.detect_charuco(g.lcd_pattern(), g.lcd_board())
        self.assertEqual(len(ids), 31 * 17)


class Metrics(unittest.TestCase):
    def test_each_control_shows_up_in_its_own_number(self):
        # term in the truth field, its coefficient, the number it must show as
        cases = {"pin": ("pincushion", 3.0, 3.0), "trap": ("trapezoid", 2.0, 8.0),
                 "pb": ("pinbalance", 1.0, 1.0), "tilt": ("tilt", 1.0, 2.0),
                 "para": ("parallelogram", 1.0, 2.0)}
        for term, (key, coef, want) in cases.items():
            m = g.metrics(s.poly_field(**{term: coef}))
            self.assertAlmostEqual(m[key], want, places=6, msg=term)
            for other in ALL[:-4]:
                if other != key:
                    self.assertAlmostEqual(m[other], 0.0, places=6, msg=f"{term} leaked into {other}")

    def test_top_corner_flare_goes_to_sidepin_top(self):
        m = g.metrics(s.poly_field(corner_top=2.0))
        self.assertGreater(m["sidepin_top"], 1.4)
        self.assertLess(abs(m["sidepin_bottom"]), 0.25)
        self.assertLess(abs(m["pincushion"]), 0.4)

    def test_instruction_uses_learnt_step_size(self):
        m = g.metrics(s.poly_field(pin=2.0))
        _, first = g.report(m, False)
        self.assertIn("Pincushion", first)
        self.assertIn("+5", first)
        _, then = g.report(m, False, {"pincushion": -0.4})
        self.assertIn("Pincushion +5 steps", then)


class Photos(unittest.TestCase):
    TRUTH = staticmethod(s.poly_field(pin=-2.0, pb=0.8, trap=-1.5, tilt=0.7, para=0.6, vlin=0.5,
                         corner_top=1.0, hsize=-3.0, vpos=1.5))

    @classmethod
    def setUpClass(cls):
        cls.lens = dict(k=(0.06, -0.12), f=0.85 * 4000)
        cls.cam = s.camera(dist_mm=750, yaw=6, pitch=-5, roll=1.5, offset=(40, -30), **cls.lens)
        cls.photo = s.crt_photo(cls.TRUTH, cls.cam, depth=18.0)
        cls.want = g.metrics(cls.TRUTH)

    def check(self, got, keys, tol):
        for k in keys:
            self.assertLess(abs(got[k] - self.want[k]), tol, f"{k}: got {got[k]:.2f}, "
                            f"true {self.want[k]:.2f}")

    def test_shape_without_markers(self):
        r = g.analyse(self.photo, cam_dict(self.cam))
        self.assertFalse(r["with_markers"])
        self.check(r["metrics"], SHAPE, 0.2)

    def test_everything_with_markers_and_depth(self):
        r = g.analyse(self.photo, cam_dict(self.cam), g.bezel_positions(460, 350), 18.0)
        self.assertTrue(r["with_markers"])
        self.check(r["metrics"], SHAPE + ("tilt", "parallelogram", "vlin_corner"), 0.25)
        self.check(r["metrics"], ("hsize", "vsize", "hpos", "vpos"), 0.8)

    def test_lcd_calibration_recovers_the_lens(self):
        views = [s.camera(dist_mm=d, yaw=y, pitch=p, roll=rl, offset=o, **self.lens)
                 for d, y, p, rl, o in [(650, 0, 0, 0, (0, 0)), (600, 18, 0, 2, (150, 0)),
                                        (620, 0, 15, 1, (0, 100)), (550, -10, -12, -3, (-80, -60))]]
        c = g.calibrate([(f"v{i}", s.lcd_photo(v, seed=i)) for i, v in enumerate(views)])
        self.assertEqual(c["views"], 4)
        self.assertLess(max(c["flat_rms_px"]), 0.15)
        cam = {"K": np.array(c["K"]), "dist": np.array(c["dist"]), "size": tuple(c["size"]),
               "views": 4}
        r = g.analyse(self.photo, cam)
        self.check(r["metrics"], SHAPE, 0.25)

    def test_blurred_and_cropped_photos_are_rejected(self):
        truth = s.poly_field(pin=1.0)
        with self.assertRaises(g.BadPhoto):
            g.analyse(s.crt_photo(truth, s.camera(w=3000, h=2250, f=2250), blur=5), None)
        cropped = s.camera(w=3000, h=2250, f=2250, dist_mm=500, offset=(-150, 0))
        with self.assertRaises(g.BadPhoto):
            g.analyse(s.crt_photo(truth, cropped), None)

    def test_rough_photo_measures_or_is_rejected(self):
        rough = dict(blur=1.5, bloom=0.4, grille=0.5, scan=0.25, noise=10, jpeg=75, seed=3)
        near = s.camera(dist_mm=650, yaw=-4, pitch=4)
        r = g.analyse(s.crt_photo(self.TRUTH, near, **rough), cam_dict(near))
        self.check(r["metrics"], SHAPE, max(0.2, r["uncertainty_mm"]))
        # from further back the moiré bands bias the edges: refuse, don't guess
        far = s.camera(dist_mm=850, yaw=-4, pitch=4)
        with self.assertRaises(g.BadPhoto):
            g.analyse(s.crt_photo(self.TRUTH, far, **rough), cam_dict(far))

if __name__ == "__main__":
    unittest.main()

"""ChArUco photo mode: measure the iiyama Vision Master Pro 514's geometry
from a phone photo of a ChArUco board shown fullscreen at 1600x1200.

    python -m iiyama.charuco_geo patterns
    python -m iiyama.charuco_geo calibrate lcd1.jpg lcd2.jpg ... -o camera.json
    python -m iiyama.charuco_geo analyse crt.jpg --camera camera.json -o round1
        [--bezel 440,330] [--depth 20] [--prev round0.json --changed pincushion=+5]

Screen coordinates: u, v run -1..1 across the active raster (left/top = -1).
The nominal picture is 395 x 295 mm (the manual's H/V size). The measured
raster is "nominal + field", where the field (dx, dy in mm) is a smooth
polynomial fitted to every detected corner. All the numbers reported are read
off that field along the four picture edges.

Without bezel markers the camera pose is unknown, so everything a camera
tilt can mimic (size, position, tilt, trapezoid, parallelogram, V-lin top vs
bottom) is left out and reported as "needs markers". Pincushion, pin-balance,
the corner controls and V-lin side do not depend on the pose.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import cv2
import numpy as np
from numpy.polynomial import legendre as L

HERE = os.path.dirname(os.path.abspath(__file__))
PATTERNS = os.path.join(HERE, "patterns")

ARUCO = cv2.aruco
DICT = ARUCO.DICT_5X5_1000
MARKER_RATIO = 0.7
CRT_W, CRT_H, CRT_SQ = 1600, 1200, 50
WHITE = 180                      # ~70% grey: less bloom and ABL than 255
LCD_W, LCD_H, LCD_SQ, LCD_ID0 = 3840, 2160, 120, 500
BEZEL_DICT = ARUCO.DICT_4X4_50   # ids 0..3 = top-left, top-right, bottom-right, bottom-left
BEZEL_NAMES = ("top-left", "top-right", "bottom-right", "bottom-left")
A, B = 395.0 / 2, 295.0 / 2      # nominal half width / height, mm
DEG = 4                          # field polynomial degree per axis
TOL = 0.5                        # mm, the manual's side-distortion figure


# ----------------------------------------------------------------- boards

def _dict():
    return ARUCO.getPredefinedDictionary(DICT)


def crt_board(sq=CRT_SQ):
    return ARUCO.CharucoBoard((CRT_W // sq, CRT_H // sq), float(sq),
                              float(round(sq * MARKER_RATIO)), _dict())


def lcd_board():
    nx, ny = LCD_W // LCD_SQ, LCD_H // LCD_SQ
    ids = np.arange(LCD_ID0, LCD_ID0 + nx * ny // 2)
    return ARUCO.CharucoBoard((nx, ny), float(LCD_SQ), float(round(LCD_SQ * MARKER_RATIO)),
                              _dict(), ids)


def crt_pattern(sq=CRT_SQ, white=WHITE):
    """The CRT board as a 1600x1200 greyscale image, 1 px outline on the edge."""
    img = crt_board(sq).generateImage((CRT_W, CRT_H), marginSize=0, borderBits=1)
    img = np.where(img > 127, white, 0).astype(np.uint8)
    img[0, :] = img[-1, :] = img[:, 0] = img[:, -1] = white
    return img


def lcd_pattern():
    img = lcd_board().generateImage((LCD_W, LCD_H), marginSize=0, borderBits=1)
    return np.where(img > 127, 255, 0).astype(np.uint8)


def bezel_marker_sheet(dpi=300, mm=40):
    """A4 sheet with the four bezel markers (print at 100%, cut out)."""
    px = lambda v: int(round(v / 25.4 * dpi))
    sheet = np.full((px(297), px(210)), 255, np.uint8)
    d = ARUCO.getPredefinedDictionary(BEZEL_DICT)
    for k, name in enumerate(BEZEL_NAMES):
        m = ARUCO.generateImageMarker(d, k, px(mm), borderBits=1)
        x0 = px(25 + (k % 2) * 95)
        y0 = px(30 + (k // 2) * 120)
        sheet[y0:y0 + m.shape[0], x0:x0 + m.shape[1]] = m
        cv2.putText(sheet, name, (x0, y0 + m.shape[0] + px(8)), cv2.FONT_HERSHEY_SIMPLEX,
                    2.2, 0, 4, cv2.LINE_AA)
    # 100 mm check bar: measure it after printing
    y = px(275)
    cv2.rectangle(sheet, (px(55), y), (px(155), y + px(3)), 0, -1)
    cv2.putText(sheet, "this bar must measure 100 mm", (px(55), y - px(3)),
                cv2.FONT_HERSHEY_SIMPLEX, 2.0, 0, 4, cv2.LINE_AA)
    return sheet


def write_patterns(out=PATTERNS):
    os.makedirs(out, exist_ok=True)
    grey = crt_pattern()
    files = {"charuco_crt_1600x1200.png": cv2.cvtColor(grey, cv2.COLOR_GRAY2BGR),
             "charuco_lcd_3840x2160.png": cv2.cvtColor(lcd_pattern(), cv2.COLOR_GRAY2BGR),
             "bezel_markers_A4_print_100pct.png": bezel_marker_sheet()}
    for name, ch in (("red", 2), ("green", 1), ("blue", 0)):
        img = np.zeros((CRT_H, CRT_W, 3), np.uint8)
        img[..., ch] = crt_pattern(white=255)
        files[f"charuco_crt_1600x1200_{name}.png"] = img
    for name, img in files.items():
        cv2.imwrite(os.path.join(out, name), img)
    return sorted(files)


# ---------------------------------------------------------------- detection

def _gray(img):
    return img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def detect_charuco(img, board):
    """Detected corners (N,2) in image pixel-centre coords and their ids (N,)."""
    params = ARUCO.DetectorParameters()
    params.adaptiveThreshWinSizeMax = 53
    params.minMarkerPerimeterRate = 0.005   # default 0.03 drops small markers in 12 MP photos
    cp = ARUCO.CharucoParameters()
    cp.tryRefineMarkers = True
    det = ARUCO.CharucoDetector(board, cp, params)
    gray = _gray(img)
    cc, ci, _, _ = det.detectBoard(gray)
    if ci is None or len(ci) == 0:
        return np.zeros((0, 2)), np.zeros(0, int)
    pts, ids = cc.reshape(-1, 2).astype(np.float64), ci.flatten().astype(int)
    return refine(gray, pts, ids, board), ids


def refine(gray, pts, ids, board):
    """Re-refine each corner with a small window (a fraction of the square as
    seen in the photo), so marker edges near the corner don't pull it."""
    obj = board.getChessboardCorners()[ids, :2]
    if len(ids) < 4:
        return pts
    H, _ = cv2.findHomography(obj, pts, 0)
    J = cv2.perspectiveTransform((obj + [board.getSquareLength(), 0]).reshape(-1, 1, 2), H)
    sq = np.median(np.linalg.norm(J.reshape(-1, 2) - pts, axis=1))
    win = int(np.clip(round(0.09 * sq), 2, 15))
    g = cv2.GaussianBlur(gray, (0, 0), max(0.6, 0.02 * sq)).astype(np.float32)
    p = cv2.cornerSubPix(g, pts.astype(np.float32).reshape(-1, 1, 2), (win, win), (-1, -1),
                         (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-4))
    p = p.reshape(-1, 2).astype(np.float64)
    moved = np.linalg.norm(p - pts, axis=1) > 0.2 * sq
    p[moved] = pts[moved]
    return p


def detect_bezel(img):
    """Corners of bezel markers 0..3 that were found, {id: (4, 2) array}."""
    params = ARUCO.DetectorParameters()
    params.minMarkerPerimeterRate = 0.005
    det = ARUCO.ArucoDetector(ARUCO.getPredefinedDictionary(BEZEL_DICT), params)
    corners, ids, _ = det.detectMarkers(_gray(img))
    raw = {}
    if ids is None:
        return raw
    for c, i in zip(corners, ids.flatten()):
        if 0 <= i <= 3:
            c = c.reshape(4, 2).astype(np.float64)
            raw[int(i)] = c
    return raw


def _diag_centre(c):
    """Intersection of a quad's diagonals (the true centre under perspective)."""
    p1, p3, p2, p4 = c[0], c[2], c[1], c[3]
    d1, d2 = p3 - p1, p4 - p2
    m = np.array([d1, -d2]).T
    s = np.linalg.solve(m, p2 - p1)[0]
    return p1 + s * d1


# --------------------------------------------------------------- camera

def load_camera(path):
    if not path:
        return None
    with open(path) as f:
        c = json.load(f)
    return {"K": np.array(c["K"]), "dist": np.array(c["dist"]), "size": tuple(c["size"]),
            "views": c.get("views", 1)}


def undistort(pts, cam):
    if cam is None or len(pts) == 0:
        return np.asarray(pts, np.float64)
    return undistort_raw(pts, cam["K"], cam["dist"], cam["K"])


def undistort_raw(pts, K, dist, P=None, iters=50):
    """cv2.undistortPoints with enough iterations for strong phone distortion
    (OpenCV 4 calls this undistortPointsIter; 5 takes the criteria directly)."""
    p = np.asarray(pts, np.float64).reshape(-1, 1, 2)
    crit = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, iters, 1e-9)
    if hasattr(cv2, "undistortPointsIter"):
        out = cv2.undistortPointsIter(p, K, dist, None, P, crit)
    else:
        out = cv2.undistortPoints(p, K, dist, None, None, P, crit)
    return out.reshape(-1, 2)


def calibrate(images):
    """Camera intrinsics + lens distortion from photos of the LCD board."""
    board = lcd_board()
    obj_all = board.getChessboardCorners()
    objs, imgs, notes, size = [], [], [], None
    for name, img in images:
        h, w = img.shape[:2]
        if size is None:
            size = (w, h)
        if (w, h) != size:
            notes.append(f"{name}: different size {w}x{h}, skipped")
            continue
        pts, ids = detect_charuco(img, board)
        if len(ids) < 40:
            notes.append(f"{name}: only {len(ids)} corners found, skipped")
            continue
        objs.append(obj_all[ids].astype(np.float32))
        imgs.append(pts.astype(np.float32).reshape(-1, 1, 2))
        notes.append(f"{name}: {len(ids)} corners")
    if not objs:
        raise ValueError("no usable LCD photo")
    w, h = size
    K0 = np.array([[0.75 * w, 0, (w - 1) / 2], [0, 0.75 * w, (h - 1) / 2], [0, 0, 1]])
    if len(objs) >= 4:
        flags = cv2.CALIB_FIX_K3
    else:   # too few views to pin the focal length: keep it at a phone-like guess
        flags = (cv2.CALIB_USE_INTRINSIC_GUESS | cv2.CALIB_FIX_PRINCIPAL_POINT |
                 cv2.CALIB_FIX_FOCAL_LENGTH | cv2.CALIB_ZERO_TANGENT_DIST | cv2.CALIB_FIX_K3)
    rms, K, dist, _, _ = cv2.calibrateCamera(objs, imgs, size, K0, None, flags=flags)
    # straightness check: after undistortion each view should be a pure homography
    cam = {"K": K, "dist": dist.ravel(), "size": size}
    resid = []
    for o, i in zip(objs, imgs):
        u = undistort(i.reshape(-1, 2), cam)
        H, _ = cv2.findHomography(o[:, :2], u, 0)
        p = cv2.perspectiveTransform(o[:, :2].reshape(-1, 1, 2).astype(np.float64), H).reshape(-1, 2)
        resid.append(np.sqrt(np.mean(np.sum((p - u) ** 2, 1))))
    return {"K": K.tolist(), "dist": dist.ravel().tolist(), "size": list(size),
            "rms_px": float(rms), "flat_rms_px": [float(r) for r in resid],
            "views": len(objs), "notes": notes}


# ----------------------------------------------------------------- field

TERMS = [(i, j) for i in range(DEG + 1) for j in range(DEG + 1)]
# Without markers the camera homography (8 numbers) is fitted too, so 8 field
# terms must be held at zero: the 6 affine ones, and for the 2 perspective
# ones the vertical terms (y ~ u*v, y ~ v^2). So a camera pitch and a V-lin
# corner error look the same; trapezoid is then only right if V-lin corner is.
GAUGE_X = {(0, 0), (1, 0), (0, 1)}
GAUGE_Y = {(0, 0), (1, 0), (0, 1), (1, 1), (0, 2)}


def _leg(n, t):
    c = np.zeros(n + 1)
    c[n] = 1
    return L.legval(t, c)


def _basis(u, v, terms):
    return np.stack([_leg(i, u) * _leg(j, v) for i, j in terms], 1)


class Field:
    def __init__(self, tx, cx, ty, cy):
        self.tx, self.cx, self.ty, self.cy = tx, np.asarray(cx), ty, np.asarray(cy)

    def __call__(self, u, v):
        u, v = np.asarray(u, float), np.asarray(v, float)
        sh = u.shape
        u, v = u.ravel(), v.ravel()
        dx = _basis(u, v, self.tx) @ self.cx if self.tx else np.zeros_like(u)
        dy = _basis(u, v, self.ty) @ self.cy if self.ty else np.zeros_like(u)
        return dx.reshape(sh), dy.reshape(sh)


def ideal_uv(ids, board=None):
    board = board or crt_board()
    p = board.getChessboardCorners()[ids, :2]
    return p[:, 0] / (CRT_W / 2) - 1, p[:, 1] / (CRT_H / 2) - 1


def _apply_h(H, q):
    p = np.c_[q, np.ones(len(q))] @ H.T
    return p[:, :2] / p[:, 2:3]


def fit_free(u, v, img_pts):
    """No markers: fit a homography (unknown camera pose) and the field
    jointly, with the pose-like terms held at zero."""
    from scipy.optimize import least_squares
    tx = [t for t in TERMS if t not in GAUGE_X]
    ty = [t for t in TERMS if t not in GAUGE_Y]
    Bx, By = _basis(u, v, tx), _basis(u, v, ty)
    H0, _ = cv2.findHomography(np.c_[u, v], img_pts, 0)
    H0 = H0 / H0[2, 2]
    nx = len(tx)

    def model(p):
        H = np.append(p[:8], 1).reshape(3, 3)
        X = u + Bx @ p[8:8 + nx] / A
        Y = v + By @ p[8 + nx:] / B
        return _apply_h(H, np.c_[X, Y])

    p0 = np.r_[H0.ravel()[:8], np.zeros(nx + len(ty))]
    r = least_squares(lambda p: (model(p) - img_pts).ravel(), p0, x_scale="jac", method="lm")
    H = np.append(r.x[:8], 1).reshape(3, 3)
    field = Field(tx, r.x[8:8 + nx], ty, r.x[8 + nx:])
    resid = model(r.x) - img_pts
    # residual in screen mm: image px -> mm via the local scale of H
    mm_per_px = 2 * A / np.linalg.norm(_apply_h(H, np.array([[1.0, 0]])) - _apply_h(H, np.array([[-1.0, 0]])))
    return field, H, resid * mm_per_px


def fit_markers(u, v, mm_pts):
    """Markers: screen points are known in mm; fit every term."""
    Bm = _basis(u, v, TERMS)
    dx = mm_pts[:, 0] - A * u
    dy = mm_pts[:, 1] - B * v
    cx, *_ = np.linalg.lstsq(Bm, dx, rcond=None)
    cy, *_ = np.linalg.lstsq(Bm, dy, rcond=None)
    field = Field(TERMS, cx, TERMS, cy)
    resid = np.c_[dx - Bm @ cx, dy - Bm @ cy]
    return field, resid


# --------------------------------------------------------------- bezel

def bezel_positions(width_mm, height_mm):
    """Marker centres in mm (origin at their middle), for a rectangle."""
    w, h = width_mm / 2, height_mm / 2
    return {0: (-w, -h), 1: (w, -h), 2: (w, h), 3: (-w, h)}


def screen_from_image(pts, markers_img, markers_mm, cam, depth):
    """Map undistorted image points to mm on the screen plane.
    With camera intrinsics the marker pose is solved in 3D and rays are cut
    at the phosphor depth; without them a plain homography (depth ignored)."""
    ids = sorted(markers_img)
    ip = np.array([markers_img[i] for i in ids], np.float64)
    mp = np.array([markers_mm[i] for i in ids], np.float64)
    if cam is None or depth == 0:
        H, _ = cv2.findHomography(ip, mp, 0)
        return _apply_h(H, pts), None
    obj = np.c_[mp, np.zeros(len(mp))]
    ok, rvec, tvec = cv2.solvePnP(obj, ip, cam["K"], None, flags=cv2.SOLVEPNP_IPPE)
    R, _ = cv2.Rodrigues(rvec)
    C = (-R.T @ tvec).ravel()
    rays = np.c_[pts, np.ones(len(pts))] @ np.linalg.inv(cam["K"]).T @ R  # world dirs
    s = (depth - C[2]) / rays[:, 2]
    X = C[None, :] + s[:, None] * rays
    return X[:, :2], C


# ---------------------------------------------------------------- metrics

def metrics(field):
    """Plain-language numbers (mm) read off the field along the picture edges."""
    t = np.linspace(-1, 1, 201)
    one = np.ones_like(t)
    dxR, _ = field(one, t)
    dxL, _ = field(-one, t)
    _, dyT = field(t, -one)
    _, dyB = field(t, one)
    width, centre = 2 * A + dxR - dxL, (dxR + dxL) / 2
    height, midy = 2 * B + dyB - dyT, (dyT + dyB) / 2
    # Size, trapezoid, pincushion etc. act on the whole side, so read them from
    # a parabola through the middle 60% of each side; the corner controls get
    # only what the corners do beyond that parabola.
    mid = np.abs(t) <= 0.6

    def split(curve):
        c2, c1, c0 = np.polyfit(t[mid], curve[mid], 2)
        par = lambda x: c0 + c1 * x + c2 * x * x
        return c1, c2, curve[0] - par(-1.0), curve[-1] - par(1.0)
    w1, w2, w_top, w_bot = split(width)
    c1, c2, c_top, c_bot = split(centre)
    m1, m2, _, _ = split(midy)
    h1, h2, _, _ = split(height)
    theta = m1 / A
    shear = c1 / B + theta

    def cell(vc):   # height of a 100 px crosshatch cell centred at vc, on the centre line
        d = 100 / CRT_H * 2 / 2
        y = lambda vv: B * vv + field(np.array([0.0]), np.array([vv]))[1][0]
        return y(vc + d) - y(vc - d)
    top, mids, bot = cell(-11 / 12), cell(1 / 12), cell(11 / 12)
    return {
        "pincushion": w2 / 2,
        "sidepin_top": w_top / 2,
        "sidepin_bottom": w_bot / 2,
        "trapezoid": 2 * w1,
        "pinbalance": c2,
        "pinbalance_top": c_top,
        "pinbalance_bottom": c_bot,
        "tilt": 2 * m1,
        "parallelogram": shear * 2 * B,
        "vlin_side": mids - (top + bot) / 2,
        "vlin_corner": top - bot,
        "vlin_cells": [top, mids, bot],
        "topbottom_bow": h2 / 2,
        "hsize": float(width[100]),
        "vsize": float(height[100]),
        "hpos": float(centre[100]),
        "vpos": float(midy[100]),
    }


# key, control on the OSD, tolerance, needs markers, describe(value)
def _dir(v, pos, neg):
    return pos if v > 0 else neg


CHECKS = [
    ("vlin_side", "V linear side", TOL, False,
     lambda v: f"middle squares {abs(v):.1f} mm {_dir(v, 'taller', 'shorter')} than top/bottom"),
    ("vlin_corner", "V linear corner", TOL, True,
     lambda v: f"top squares {abs(v):.1f} mm {_dir(v, 'taller', 'shorter')} than bottom"),
    ("tilt", "Tilt", TOL, True,
     lambda v: f"picture turned: right end {abs(v):.1f} mm {_dir(v, 'lower', 'higher')} than left"),
    ("pincushion", "Pincushion", TOL, False,
     lambda v: f"sides {_dir(v, 'bow inwards', 'bulge outwards')} {abs(v):.1f} mm (middle vs corners)"),
    ("trapezoid", "Trapezoid", TOL, "estimate",
     lambda v: f"bottom {abs(v):.1f} mm {_dir(v, 'wider', 'narrower')} than top"),
    ("parallelogram", "Parallelogram", TOL, True,
     lambda v: f"sides lean: bottom {abs(v):.1f} mm to the {_dir(v, 'right', 'left')} of top"),
    ("pinbalance", "Pin-balance", TOL, False,
     lambda v: f"both sides bend like {_dir(v, '( (', ') )')} by {abs(v):.1f} mm"),
    ("sidepin_top", "Sidepin Top", TOL, False,
     lambda v: f"top corners {_dir(v, 'flare out', 'pull in')} {abs(v):.1f} mm beyond the side's curve"),
    ("sidepin_bottom", "Sidepin Bottom", TOL, False,
     lambda v: f"bottom corners {_dir(v, 'flare out', 'pull in')} {abs(v):.1f} mm beyond the side's curve"),
    ("pinbalance_top", "Pinbalance Top", TOL, False,
     lambda v: f"top corners both kick {abs(v):.1f} mm {_dir(v, 'right', 'left')}"),
    ("pinbalance_bottom", "Pinbalance Btm", TOL, False,
     lambda v: f"bottom corners both kick {abs(v):.1f} mm {_dir(v, 'right', 'left')}"),
]
SIZE_CHECKS = [
    ("hsize", "H-size", 395.0, 4.0), ("vsize", "V-size", 295.0, 4.0),
    ("hpos", "H-position", 0.0, 2.0), ("vpos", "V-position", 0.0, 2.0),
]


def report(m, with_markers, steps=None):
    """Lines for each check and the one next instruction."""
    steps = steps or {}
    lines, todo = [], []
    for key, ctl, tol, need, say in CHECKS:
        if need is True and not with_markers:
            lines.append((ctl, "needs bezel markers", "n/a"))
            continue
        v = m[key]
        ok = abs(v) <= tol
        extra = " (if V-lin corner is right)" if need == "estimate" and not with_markers else ""
        lines.append((ctl, say(v) + extra, "OK" if ok else "FIX"))
        if not ok:
            todo.append((key, ctl, v, 0.0))
    for key, ctl, target, tol in SIZE_CHECKS:
        if not with_markers:
            lines.append((ctl, "needs bezel markers", "n/a"))
            continue
        v = m[key]
        ok = abs(v - target) <= tol
        lines.append((ctl, f"{v:.1f} mm (target {target:.0f} +/- {tol:.0f})", "OK" if ok else "FIX"))
        if not ok:
            todo.append((key, ctl, v, target))
    if not todo:
        return lines, "Everything measurable is within tolerance."
    key, ctl, v, target = todo[0]
    rate = steps.get(key)
    if rate:
        n = int(round((target - v) / rate))
        if n == 0:
            return lines, f"{ctl}: remaining error is under one step. Leave it."
        return lines, f"Next: {ctl} {n:+d} steps  ({(target - v) / rate:+.1f} by my estimate)"
    return lines, (f"Next: {ctl}. I don't know its step size yet: change it by +5 "
                   f"steps, then send a photo so I can learn direction and size.")


# --------------------------------------------------------------- analysis

class BadPhoto(Exception):
    pass


def analyse(img, cam=None, bezel_mm=None, depth=0.0, board=None):
    board = board or crt_board()
    h, w = img.shape[:2]
    if cam is not None and tuple(cam["size"]) != (w, h):
        raise BadPhoto(f"photo is {w}x{h} but the LCD calibration was {cam['size'][0]}x"
                       f"{cam['size'][1]}: use the same camera and zoom, no cropping")
    pts, ids = detect_charuco(img, board)
    n_all = len(board.getChessboardCorners())
    if len(ids) < 0.6 * n_all:
        raise BadPhoto(f"only {len(ids)} of {n_all} corners found (need 60%): blurred, "
                       "too dark/bright, or moiré. Refocus, lower exposure, step back a little.")
    u, v = ideal_uv(ids, board)
    for name, sel in (("left", u < -0.9), ("right", u > 0.9), ("top", v < -0.9), ("bottom", v > 0.9)):
        if sel.sum() < 0.6 * (23 if name in ("left", "right") else 31):   # outermost line
            raise BadPhoto(f"the {name} edge of the board is missing: the edges matter most")
    und = undistort(pts, cam)
    notes = []
    if cam is None:
        notes.append("no LCD calibration: lens bending is mixed into pincushion")
    raw = detect_bezel(img)
    with_markers = bezel_mm is not None and len(raw) == 4
    if bezel_mm is not None and not with_markers:
        notes.append(f"bezel markers found: {sorted(raw)} of 0-3; measuring shape only")
    if with_markers and depth and (cam is None or cam.get("views", 1) < 4):
        notes.append("depth correction needs a 4+ view LCD calibration: sizes read ~2-3% small")
        depth = 0.0
    if with_markers:
        mk = {i: _diag_centre(undistort(c, cam)) for i, c in raw.items()}
        mm, C = screen_from_image(und, mk, bezel_mm, cam, depth)
        if C is not None:
            ang = np.degrees(np.arctan2(np.hypot(C[0], C[1]), -C[2]))
            if ang > 25:
                raise BadPhoto(f"camera {ang:.0f} degrees off-axis (max 25): shoot from straight in front")
        fit = lambda k: fit_markers(u[k], v[k], mm[k])
    else:
        fit = lambda k: (lambda f, H, r: (f, r))(*fit_free(u[k], v[k], und[k]))
    # fit, drop corners that sit far off the smooth surface (glints, misreads), refit
    keep = np.ones(len(u), bool)
    for _ in range(3):
        field, resid = fit(keep)
        e = np.sqrt(np.sum(resid ** 2, 1))
        lim = max(4 * 1.4826 * np.median(e), 0.05)
        if (e <= lim).all():
            break
        idx = np.flatnonzero(keep)
        keep[idx[e > lim]] = False
    dropped = int((~keep).sum())
    if dropped > 0.1 * len(u):
        raise BadPhoto(f"{dropped} corners disagree with the rest: moiré or glare. "
                       "Retake a little closer or further, room lights off.")
    rms = float(np.sqrt(np.mean(np.sum(resid ** 2, 1))))
    if os.environ.get("CHARUCO_DEBUG"):
        e = np.sqrt(np.sum(resid ** 2, 1))
        print("resid mm pct50/90/99/max", np.percentile(e, [50, 90, 99, 100]).round(3))
    # Moiré between the grille and the sensor shifts corners in bands, so the
    # errors are correlated; synthetic tests put the edge error at ~3x the rms.
    unc = 3 * rms
    if rms > 0.15:
        raise BadPhoto(f"corners scatter {rms:.2f} mm (edge numbers would be +/-{unc:.1f} mm): "
                       "blur or moiré. Move closer so the monitor fills the frame, brace "
                       "the phone, retake.")
    if unc > 0.25:
        notes.append(f"numbers are +/-{unc:.1f} mm: a closer, sharper photo would help")
    m = metrics(field)
    return {"metrics": m, "with_markers": with_markers, "corners": int(keep.sum()),
            "corners_total": n_all, "fit_rms_mm": rms, "uncertainty_mm": unc, "notes": notes,
            "field": {"tx": field.tx, "cx": list(map(float, field.cx)),
                      "ty": field.ty, "cy": list(map(float, field.cy))},
            "dropped": dropped, "u": u[keep].tolist(), "v": v[keep].tolist()}


def field_from(res):
    f = res["field"]
    return Field([tuple(t) for t in f["tx"]], f["cx"], [tuple(t) for t in f["ty"]], f["cy"])


# ------------------------------------------------------------- drawing

def annotate(res, lines, instruction, path):
    W, Hc = 1500, 900
    img = np.full((Hc, W, 3), 24, np.uint8)
    s = 2.3                                   # px per mm
    ox, oy = 40 + A * s, 90 + B * s
    field = field_from(res)
    t = np.linspace(-1, 1, 81)
    one = np.ones_like(t)
    edges = [(one, t), (-one, t), (t, -one), (t, one)]
    peak = max(max(np.abs(field(a, b)[0]).max(), np.abs(field(a, b)[1]).max()) for a, b in edges)
    gain = float(np.clip(25 / (max(peak, 1e-6) * s), 1, 50))
    P = lambda x, y: (int(round(ox + x * s)), int(round(oy + y * s)))
    cv2.rectangle(img, P(-A, -B), P(A, B), (90, 90, 90), 1, cv2.LINE_AA)
    for a, b in edges:
        dx, dy = field(a, b)
        pts = np.array([P(A * x + gain * p, B * y + gain * q)
                        for x, y, p, q in zip(a, b, dx, dy)], np.int32)
        cv2.polylines(img, [pts], False, (60, 200, 255), 2, cv2.LINE_AA)
    g = np.linspace(-1, 1, 9)
    for x in g:
        for y in g:
            dx, dy = field(np.array([x]), np.array([y]))
            p0 = P(A * x, B * y)
            p1 = P(A * x + gain * dx[0], B * y + gain * dy[0])
            cv2.arrowedLine(img, p0, p1, (80, 220, 80), 1, cv2.LINE_AA, tipLength=0.3)
    txt = lambda s_, x, y, c=(230, 230, 230), sc=0.55: cv2.putText(
        img, s_, (x, y), cv2.FONT_HERSHEY_SIMPLEX, sc, c, 1, cv2.LINE_AA)
    txt(f"grey = perfect 395 x 295 mm.  orange = your picture's edges, errors drawn x{gain:.0f}",
        40, 30)
    txt(f"green arrows = where each part of the picture has moved (x{gain:.0f})", 40, 55)
    x0 = 40 + int(2 * A * s) + 40
    y = 110
    col = {"OK": (120, 220, 120), "FIX": (80, 120, 255), "n/a": (150, 150, 150)}
    for ctl, say, st in lines:
        txt(f"{st:>3}", x0, y, col[st])
        txt(ctl, x0 + 45, y, (230, 230, 230))
        txt(say, x0 + 45, y + 20, (170, 170, 170), 0.45)
        y += 46
    txt(instruction, 40, Hc - 25, (80, 200, 255), 0.65)
    m = res["metrics"]["vlin_cells"]
    txt(f"centre-line 100px squares: top {m[0]:.2f}  middle {m[1]:.2f}  bottom {m[2]:.2f} mm"
        f"   | corners {res['corners']}/{res['corners_total']}, numbers +/-{res['uncertainty_mm']:.2f} mm",
        40, Hc - 60, (170, 170, 170), 0.5)
    cv2.imwrite(path, img)


# ------------------------------------------------------------------- CLI

def _read(path):
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"cannot read {path}")
    return img


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("patterns")
    c = sub.add_parser("calibrate")
    c.add_argument("photos", nargs="+")
    c.add_argument("-o", default="camera.json")
    a = sub.add_parser("analyse")
    a.add_argument("photo")
    a.add_argument("--camera")
    a.add_argument("--bezel", help="marker centre spacing W,H in mm")
    a.add_argument("--depth", type=float, default=0.0, help="markers to phosphor, mm")
    a.add_argument("--steps", default=None, help="json of learnt mm per step")
    a.add_argument("--prev", help="previous round's json")
    a.add_argument("--changed", help="control=steps changed since --prev, e.g. pincushion=+5")
    a.add_argument("-o", default="round")
    args = ap.parse_args(argv)

    if args.cmd == "patterns":
        for n in write_patterns():
            print(os.path.join(PATTERNS, n))
        return
    if args.cmd == "calibrate":
        r = calibrate([(p, _read(p)) for p in args.photos])
        with open(args.o, "w") as f:
            json.dump(r, f, indent=1)
        print(json.dumps({k: r[k] for k in ("views", "rms_px", "flat_rms_px", "notes")}, indent=1))
        return
    cam = load_camera(args.camera)
    bez = None
    if args.bezel:
        wmm, hmm = (float(x) for x in args.bezel.split(","))
        bez = bezel_positions(wmm, hmm)
    try:
        res = analyse(_read(args.photo), cam, bez, args.depth)
    except BadPhoto as e:
        print("PHOTO REJECTED:", e)
        return 2
    steps_path = args.steps or os.path.join(os.path.dirname(args.o) or ".", "steps.json")
    steps = json.load(open(steps_path)) if os.path.exists(steps_path) else {}
    if args.prev and args.changed:
        key, n = args.changed.split("=")
        prev = json.load(open(args.prev))
        rate = (res["metrics"][key] - prev["metrics"][key]) / float(n)
        steps[key] = rate
        print(f"learnt: {key} moves {rate:+.3f} mm per step")
        with open(steps_path, "w") as f:
            json.dump(steps, f, indent=1)
    lines, instr = report(res["metrics"], res["with_markers"], steps)
    res["instruction"] = instr
    with open(args.o + ".json", "w") as f:
        json.dump(res, f)
    annotate(res, lines, instr, args.o + ".png")
    for ctl, say, st in lines:
        print(f"{st:>3}  {ctl:<16} {say}")
    for n in res["notes"]:
        print("note:", n)
    print(instr)


if __name__ == "__main__":
    sys.exit(main())

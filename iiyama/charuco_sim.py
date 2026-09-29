"""Synthetic phone photos of the CRT and the LCD, for testing charuco_geo.

World frame: bezel plane z = 0, x right, y down, z into the monitor, mm,
origin at the middle of the four bezel markers. The phosphor sits at z=depth.
"""
from __future__ import annotations

import cv2
import numpy as np

from . import charuco_geo as g


def camera(w=4000, h=3000, f=None, k=(0.0, 0.0), dist_mm=650.0, yaw=0.0, pitch=0.0,
           roll=0.0, offset=(0.0, 0.0)):
    f = f or 0.75 * w
    K = np.array([[f, 0, (w - 1) / 2], [0, f, (h - 1) / 2], [0, 0, 1.0]])
    R, _ = cv2.Rodrigues(np.radians([pitch, yaw, 0.0]))
    Rz, _ = cv2.Rodrigues(np.radians([0, 0, roll]))
    R = Rz @ R
    C = np.array([offset[0], offset[1], -dist_mm])
    # aim the camera at the origin: rotate the optical axis, keep C fixed
    return {"K": K, "dist": np.array([k[0], k[1], 0, 0, 0.0]), "size": (w, h), "R": R, "C": C}


ST = 4   # everything geometric is smooth: compute on a coarse grid, interpolate


def _rays(cam):
    """World ray directions on the coarse grid, shape (gh*gw, 3)."""
    w, h = cam["size"]
    gx = np.arange(0, w + ST, ST, dtype=np.float32)
    gy = np.arange(0, h + ST, ST, dtype=np.float32)
    xs, ys = np.meshgrid(gx, gy)
    n = g.undistort_raw(np.stack([xs.ravel(), ys.ravel()], 1), cam["K"], cam["dist"], None, iters=20)
    return np.c_[n, np.ones(len(n))] @ cam["R"], (len(gy), len(gx))


def _up(a, shape, cam):
    """Coarse-grid values -> full photo resolution (flat float32)."""
    w, h = cam["size"]
    a = a.reshape(shape).astype(np.float32)
    mx, my = np.meshgrid(np.arange(w, dtype=np.float32) / ST, np.arange(h, dtype=np.float32) / ST)
    return cv2.remap(a, mx, my, cv2.INTER_LINEAR).ravel()


def _hit(cam, d, z):
    s = (z - cam["C"][2]) / d[:, 2]
    return cam["C"][0] + s * d[:, 0], cam["C"][1] + s * d[:, 1]


def _finish(img, cam, blur, noise, seed, jpeg):
    w, h = cam["size"]
    img = img.reshape(h, w).astype(np.float32)
    if blur:
        img = cv2.GaussianBlur(img, (0, 0), blur)
    rng = np.random.default_rng(seed)
    img = img + rng.normal(0, noise, img.shape)
    img = np.clip(img, 0, 255).astype(np.uint8)
    if jpeg:
        _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, jpeg])
        img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def crt_photo(field=None, cam=None, depth=15.0, bezel=(460.0, 350.0), markers=True,
              pattern=None, spot=0.8, bloom=0.12, grille=0.25, scan=0.1, blur=1.0,
              noise=3.0, seed=0, jpeg=90, raster_offset=(0.0, 0.0)):
    """Render a photo. field(u, v) -> (dx, dy) mm is the true geometry error."""
    cam = cam or camera()
    pat = (pattern if pattern is not None else g.crt_pattern()).astype(np.float32)
    if spot:
        pat = cv2.GaussianBlur(pat, (0, 0), spot)
    if bloom:
        pat = pat + bloom * cv2.GaussianBlur(pat, (0, 0), 20)
    d, shape = _rays(cam)
    up = lambda a: _up(a, shape, cam)
    # phosphor plane
    X, Y = _hit(cam, d, depth)
    X, Y = X - raster_offset[0], Y - raster_offset[1]
    u, v = X / g.A, Y / g.B
    if field is not None:
        u0, v0 = u.copy(), v.copy()
        for _ in range(8):   # invert: find ideal (u, v) that lands at (X, Y)
            dx, dy = field(u, v)
            u, v = u0 - dx / g.A, v0 - dy / g.B
    bx = ((u + 1) * g.CRT_W / 2 - 0.5).astype(np.float32)
    by = ((v + 1) * g.CRT_H / 2 - 0.5).astype(np.float32)
    w, h = cam["size"]
    X, Y = up(X), up(Y)
    scr = cv2.remap(pat, up(bx).reshape(h, w), up(by).reshape(h, w), cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT, borderValue=0).ravel()
    # grille stripes and scan lines, averaged over each camera pixel's footprint
    # (a real sensor integrates over its pixel area; point sampling would
    # exaggerate the moire)
    fx = np.abs(np.gradient(X.reshape(h, w), axis=1)).ravel()
    fy = np.abs(np.gradient(Y.reshape(h, w), axis=0)).ravel()
    ln = 295.0 / 1200
    scr = scr * (1 - grille + grille * np.sinc(fx / 0.24) * np.cos(2 * np.pi * X / 0.24)) \
              * (1 - scan + scan * np.sinc(fy / ln) * np.cos(2 * np.pi * Y / ln))
    scr = scr * 1.2 + 6          # exposure; black glass is not quite black
    # bezel plane in front
    Xb, Yb = _hit(cam, d, 0.0)
    Xb, Yb = up(Xb), up(Yb)
    open_ = (np.abs(Xb) < 205) & (np.abs(Yb) < 155)
    out = np.where(open_, scr, 45.0)
    if markers:
        mk = _marker_canvas(bezel)
        s, (cw, ch) = 4.0, (mk.shape[1], mk.shape[0])
        mx = (Xb * s + cw / 2).astype(np.float32).reshape(h, w)
        my = (Yb * s + ch / 2).astype(np.float32).reshape(h, w)
        m = cv2.remap(mk, mx, my, cv2.INTER_LINEAR, borderValue=-1).ravel()
        out = np.where((m >= 0) & ~open_, m, out)
    return _finish(out, cam, blur, noise, seed, jpeg)


def _marker_canvas(bezel, size_mm=40.0, s=4.0):
    W, H = 700, 560
    can = np.full((int(H * s), int(W * s)), -1, np.float32)
    dct = cv2.aruco.getPredefinedDictionary(g.BEZEL_DICT)
    for i, (x, y) in g.bezel_positions(*bezel).items():
        px = int(size_mm * s)
        m = cv2.aruco.generateImageMarker(dct, i, px, borderBits=1).astype(np.float32)
        m = cv2.copyMakeBorder(m, px // 5, px // 5, px // 5, px // 5, cv2.BORDER_CONSTANT, value=255)
        m = m * 0.8 + 10
        cx, cy = x * s + can.shape[1] / 2, y * s + can.shape[0] / 2
        x0, y0 = int(round(cx - m.shape[1] / 2)), int(round(cy - m.shape[0] / 2))
        can[y0:y0 + m.shape[0], x0:x0 + m.shape[1]] = m
    return can


def lcd_photo(cam, pitch_mm=0.1845, blur=0.8, noise=2.0, seed=0, jpeg=92):
    pat = g.lcd_pattern().astype(np.float32)
    d, shape = _rays(cam)
    X, Y = _hit(cam, d, 0.0)
    X, Y = _up(X, shape, cam), _up(Y, shape, cam)
    w, h = cam["size"]
    bx = (X / pitch_mm + g.LCD_W / 2 - 0.5).astype(np.float32).reshape(h, w)
    by = (Y / pitch_mm + g.LCD_H / 2 - 0.5).astype(np.float32).reshape(h, w)
    img = cv2.remap(pat, bx, by, cv2.INTER_AREA, borderValue=30).ravel() * 0.8 + 10
    return _finish(img, cam, blur, noise, seed, jpeg)


def poly_field(**c):
    """A simple truth field from named terms (mm at the edge/corner)."""
    def f(u, v):
        u, v = np.asarray(u, float), np.asarray(v, float)
        dx = (c.get("pin", 0) * u * v ** 2 + c.get("trap", 0) * u * v + c.get("pb", 0) * v ** 2
              + c.get("para", 0) * v + c.get("hsize", 0) * u + c.get("hpos", 0)
              + c.get("corner_top", 0) * u * np.where(v < 0, v ** 4, 0)
              + c.get("corner_bot", 0) * u * np.where(v > 0, v ** 4, 0)
              - c.get("tilt", 0) * v * g.B / g.A)
        dy = (c.get("vlin", 0) * (v ** 3 - v) + c.get("vlin2", 0) * (v ** 2 - 1)
              + c.get("vsize", 0) * v + c.get("vpos", 0) + c.get("bow", 0) * v * u ** 2
              + c.get("tilt", 0) * u)
        return dx, dy
    return f

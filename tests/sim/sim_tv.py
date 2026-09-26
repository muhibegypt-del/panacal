"""A simulated LG OLED, pattern window and colorimeter for offline runs.

The panel follows what the worker documents about real LG sets: an 8-bit
full-range code reaches the 1024-entry 1D DPG table through LG's legal-code
ladder, the identity table is idx*32767/1023, and the TV applies an uploaded
table immediately while calibration mode is held. Each channel has its own
native gamma and gain, so the greyscale starts off target in both tint and
gamma, the way an uncalibrated panel does.
"""
from __future__ import annotations

import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LADDER_CODES = [84, 92, 100, 108, 124, 152, 196, 240, 284, 328, 372, 416, 460, 504, 544, 588,
                632, 676, 720, 764, 808, 852, 896, 932, 984, 1023]
LADDER_INDEXES = [21, 30, 38, 47, 64, 94, 141, 188, 235, 282, 329, 375, 422, 469, 512, 559,
                  606, 653, 700, 747, 794, 841, 888, 926, 981, 1023]
# BT.709 RGB -> XYZ (D65 white at Y=1).
M709 = ((0.4124564, 0.3575761, 0.1804375),
        (0.2126729, 0.7151522, 0.0721750),
        (0.0193339, 0.1191920, 0.9503041))


def rgb_to_xyz_matrix(primaries, white=(0.3127, 0.3290)):
    """RGB -> XYZ for the given xy primaries, scaled so RGB 1,1,1 is the white at Y=1."""
    cols = [(x / y, 1.0, (1 - x - y) / y) for x, y in primaries]
    wx, wy = white
    W = (wx / wy, 1.0, (1 - wx - wy) / wy)
    m = [[cols[c][r] for c in range(3)] for r in range(3)]
    det = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
           + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    inv = [[(m[(j + 1) % 3][(i + 1) % 3] * m[(j + 2) % 3][(i + 2) % 3]
             - m[(j + 1) % 3][(i + 2) % 3] * m[(j + 2) % 3][(i + 1) % 3]) / det for j in range(3)] for i in range(3)]
    scale = [sum(inv[i][k] * W[k] for k in range(3)) for i in range(3)]
    return tuple(tuple(m[r][c] * scale[c] for c in range(3)) for r in range(3))


# The G2's primaries with LG's calibration data cleared, as measured in HCFR.
NATIVE_PRIMARIES = ((0.6746, 0.3221), (0.2537, 0.6604), (0.1460, 0.0462))
M_NATIVE = rgb_to_xyz_matrix(NATIVE_PRIMARIES)


def identity(index: float) -> float:
    return index * 32767 / 1023


def legal_code(code8: int, black_level: str = "high", gpu_range: str = "full") -> float:
    """Drawn 8-bit code -> the 10-bit legal-domain code the TV samples with.

    gpu_range "limited" is a GPU squeezing 0-255 into 16-235 on the wire.
    black_level is the TV's HDMI Black Level: "high" expects full range and
    converts it (64 + c*876/1023, as the worker documents); "low" expects
    limited range, uses the code directly and clips below black and above
    white, as LG does for RGB."""
    wire = round(16 + code8 * 219 / 255) if gpu_range == "limited" else code8
    if black_level == "low":
        return float(min(940, max(64, wire << 2)))
    # 8 -> 10 bit as the worker's hardware-probed mapping assumes: a plain
    # shift, with full white landing on the last code.
    wire10 = 1023 if wire >= 255 else wire << 2
    return 64 + wire10 * 876 / 1023


def sample_index(code8: int, black_level: str = "high", gpu_range: str = "full") -> float:
    """Drawn 8-bit code -> fractional DPG sample index (via the legal ladder)."""
    legal = legal_code(code8, black_level, gpu_range)
    codes, indexes = LADDER_CODES, LADDER_INDEXES
    if legal <= codes[0]:
        slope = (indexes[1] - indexes[0]) / (codes[1] - codes[0])
        return max(0.0, indexes[0] + (legal - codes[0]) * slope)
    for k in range(len(codes) - 1):
        if codes[k] <= legal <= codes[k + 1]:
            f = (legal - codes[k]) / (codes[k + 1] - codes[k])
            return indexes[k] + (indexes[k + 1] - indexes[k]) * f
    return float(indexes[-1])


def sample_index_f(code: float, black_level: str = "high", gpu_range: str = "full") -> float:
    """sample_index for the fractional code a 3D LUT hands the 1D stage."""
    wire = 16 + code * 219 / 255 if gpu_range == "limited" else code
    if black_level == "low":
        legal = min(940.0, max(64.0, wire * 4))
    else:
        legal = 64 + (1023.0 if wire >= 255 else wire * 4) * 876 / 1023
    codes, indexes = LADDER_CODES, LADDER_INDEXES
    if legal <= codes[0]:
        slope = (indexes[1] - indexes[0]) / (codes[1] - codes[0])
        return max(0.0, indexes[0] + (legal - codes[0]) * slope)
    for k in range(len(codes) - 1):
        if codes[k] <= legal <= codes[k + 1]:
            f = (legal - codes[k]) / (codes[k + 1] - codes[k])
            return indexes[k] + (indexes[k + 1] - indexes[k]) * f
    return float(indexes[-1])


def pq_nits(signal: float) -> float:
    m1, m2 = 2610 / 16384, 2523 / 4096 * 128
    c1, c2, c3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32
    if signal <= 0:
        return 0.0
    p = signal ** (1 / m2)
    return 10000 * (max(p - c1, 0.0) / (c2 - c3 * p)) ** (1 / m1)


class Lut3D:
    """An LG 33x33x33 12-bit 3D LUT (R fastest, G, B slowest), trilinear."""

    SIZE = 33

    def __init__(self, values: list[int]):
        if len(values) != self.SIZE ** 3 * 3:
            raise ValueError("a 33^3 3D LUT has 107811 values")
        self.values = values

    @classmethod
    def read(cls, path: str) -> "Lut3D":
        import struct
        with open(path, "rb") as handle:
            data = handle.read()
        return cls(list(struct.unpack(f"<{len(data) // 2}H", data)))

    def node(self, r: int, g: int, b: int) -> tuple[float, float, float]:
        n = self.SIZE
        i = (r + g * n + b * n * n) * 3
        return tuple(v / 4095 for v in self.values[i:i + 3])

    def apply(self, rgb: tuple[float, float, float]) -> tuple[float, float, float]:
        """Tetrahedral interpolation, as display LUT hardware does: the grey
        diagonal is an edge of every tetrahedron it crosses, so an identity
        grey axis passes greys through unchanged."""
        n1 = self.SIZE - 1
        pos = [min(n1, max(0.0, c * n1)) for c in rgb]
        lo = [min(n1 - 1, int(p)) for p in pos]
        fr, fg, fb = (p - l for p, l in zip(pos, lo))
        r0, g0, b0 = lo
        c000 = self.node(r0, g0, b0)
        c111 = self.node(r0 + 1, g0 + 1, b0 + 1)
        if fr >= fg >= fb:
            path = ((fr, (1, 0, 0)), (fg, (1, 1, 0)))
        elif fr >= fb >= fg:
            path = ((fr, (1, 0, 0)), (fb, (1, 0, 1)))
        elif fb >= fr >= fg:
            path = ((fb, (0, 0, 1)), (fr, (1, 0, 1)))
        elif fg >= fr >= fb:
            path = ((fg, (0, 1, 0)), (fr, (1, 1, 0)))
        elif fg >= fb >= fr:
            path = ((fg, (0, 1, 0)), (fb, (0, 1, 1)))
        else:
            path = ((fb, (0, 0, 1)), (fg, (0, 1, 1)))
        (w1, d1), (w2, d2) = path
        w3 = min(fr, fg, fb)
        c1 = self.node(r0 + d1[0], g0 + d1[1], b0 + d1[2])
        c2 = self.node(r0 + d2[0], g0 + d2[1], b0 + d2[2])
        return tuple((1 - w1) * c000[i] + (w1 - w2) * c1[i] + (w2 - w3) * c2[i] + w3 * c111[i] for i in range(3))


class Panel:
    def __init__(self, peak: float = 150.0, gains=(1.0, 0.95, 1.08), gammas=(2.32, 2.20, 2.40),
                 black_level: str = "high", gpu_range: str = "full"):
        self.peak = peak
        self.black_level = black_level
        self.gpu_range = gpu_range
        self.gains = gains
        self.gammas = gammas
        self.dpg = [[identity(i) for i in range(1024)] for _ in range(3)]
        # LG's factory data maps BT.709 onto the panel; the SDR reference
        # reset replaces it with identity, leaving the native primaries.
        self.gamut = M709
        self.lut: Lut3D | None = None
        # HDR: in LG's calibration mode the TV shows the signal on a 2.2
        # curve against its peak (what the HDR20 path calibrates); outside
        # it, PQ through the tone map for tone_peak.
        self.hdr = False
        self.calibrating = False
        self.tone_peak = peak
        self.reference = identity(sample_index(255))  # legal white 940
        self.lock = threading.Lock()

    def table_value(self, channel: int, index: float) -> float:
        low = int(index)
        high = min(1023, low + 1)
        f = index - low
        table = self.dpg[channel]
        return table[low] * (1 - f) + table[high] * f

    def xyz(self, rgb: tuple[int, int, int]) -> tuple[float, float, float]:
        with self.lock:
            light = []
            if self.lut is None:
                indexes = [sample_index(code, self.black_level, self.gpu_range) for code in rgb]
            else:
                mapped = self.lut.apply(tuple(code / 255 for code in rgb))
                indexes = [sample_index_f(v * 255, self.black_level, self.gpu_range) for v in mapped]
            for channel, index in enumerate(indexes):
                drive = max(0.0, self.table_value(channel, index) / self.reference)
                light.append(self.gains[channel] * drive ** self.gammas[channel])
            gamut = self.gamut
        return tuple(self.peak * sum(gamut[row][c] * light[c] for c in range(3)) for row in range(3))

    def xyz_hdr(self, signal: tuple[float, float, float]) -> tuple[float, float, float]:
        """An HDR10 signal (0..1 per channel) on the panel."""
        with self.lock:
            v = tuple(signal)
            if not self.calibrating:
                # PQ -> relative to the tone-map peak (hard clip), onto the
                # 2.2 panel curve that calibration mode passes straight through.
                v = tuple(min(1.0, pq_nits(c) / self.tone_peak) ** (1 / 2.2) for c in v)
            # The 3D LUT and 1D LUT act on that panel-referred signal, which is
            # what the colour worker profiles in calibration mode (its model
            # uses the greyscale's 2.2 curve).
            if self.lut is not None:
                v = self.lut.apply(v)
            light = []
            for channel, c in enumerate(v):
                drive = max(0.0, self.table_value(channel, min(1023.0, c * 1023)) / identity(1023))
                light.append(self.gains[channel] * drive ** self.gammas[channel])
            gamut = self.gamut
        return tuple(self.peak * sum(gamut[row][c] * light[c] for c in range(3)) for row in range(3))

    def upload(self, data: list[int]) -> None:
        with self.lock:
            self.dpg = [[float(v) for v in data[c * 1024:(c + 1) * 1024]] for c in range(3)]


class SimPattern:
    def __init__(self, hdr: bool = False):
        self.rgb = (0, 0, 0)
        self.signal = (0.0, 0.0, 0.0)
        self.shown = 0
        if hdr:
            self.show_code = self._show_code      # madTPG keeps 10-bit codes

    def show(self, r: int, g: int, b: int, area: float) -> None:
        self.rgb = (r, g, b)
        self.signal = (r / 255, g / 255, b / 255)
        self.shown += 1

    def _show_code(self, r: float, g: float, b: float, input_max: int, area: float) -> None:
        top = float(input_max or 255)
        self.signal = (r / top, g / top, b / top)
        self.rgb = tuple(round(v * 255) for v in self.signal)
        self.shown += 1

    def close(self) -> None:
        pass


class SimMeter:
    """Colorimeter reading the simulated panel: 0.3% luminance and 0.05% chroma repeatability."""

    def __init__(self, panel: Panel, pattern: SimPattern, seed: int = 7):
        self.panel = panel
        self.pattern = pattern
        self.random = random.Random(seed)
        self.reads = 0

    def read(self) -> tuple[float, float, float]:
        self.reads += 1
        X, Y, Z = (self.panel.xyz_hdr(self.pattern.signal) if self.panel.hdr
                   else self.panel.xyz(self.pattern.rgb))
        # A colorimeter's repeatability is mostly common to all three
        # channels (luminance); the chromaticity is much steadier.
        common = 1 + self.random.gauss(0, 0.003)
        noise = lambda v: v * common * (1 + self.random.gauss(0, 0.0005)) + self.random.gauss(0, 0.0003)
        return noise(X), noise(Y), noise(Z)

    def restart(self) -> None:
        pass

    def close(self) -> None:
        pass


class DarkBlindMeter(SimMeter):
    """A Spyder5 in near-black: below about 0.25 cd/m2 its luminance stops
    following the patch (on the G2 it fell from 0.146 to 0.107 while the
    TV was driven several times brighter)."""

    BLIND_BELOW = 0.25

    def read(self) -> tuple[float, float, float]:
        X, Y, Z = super().read()
        if Y >= self.BLIND_BELOW:
            return X, Y, Z
        wrong = self.random.uniform(0.08, 0.16) / Y
        return X * wrong * (1 + self.random.gauss(0, 0.02)), Y * wrong, Z * wrong * (1 + self.random.gauss(0, 0.02))


class SimTV:
    """Answers pgenerator-lg requests (via stub_helper.pl) like an LG C-series."""

    MODEL = {"model_name": "OLED65C1SIM", "name": "Simulated LG OLED", "software_version": "03.20.00",
             "hello_info": {"deviceUUID": "sim-uuid", "deviceOS": "webOS", "deviceType": "tv"},
             "software_info": {"device_id": "aa:bb:cc:dd:ee:ff", "product_name": "webOSTV 6.0"}}

    FACTORY_BACKLIGHT = 80
    USER_BACKLIGHT = 55

    def __init__(self, panel: Panel, leftover_calibration: bool = True):
        self.panel = panel
        self.base_peak = panel.peak
        self.calibration_mode = False
        self.picture_mode = "expert1"
        self.backlight = self.USER_BACKLIGHT
        if leftover_calibration:
            # An earlier calibration left a blue-heavy 1D LUT behind.
            panel.dpg[2] = [v * 1.06 for v in panel.dpg[2]]
        self.wb = {"whiteBalanceRed": [0] * 26, "whiteBalanceGreen": [0] * 26,
                   "whiteBalanceBlue": [0] * 26, "adjustingLuminance": [0] * 26}
        self.requests: list[str] = []
        self.uploads = 0
        self.lut_uploads = 0
        self.tone_maps = 0

    @property
    def calibration_mode(self) -> bool:
        return self._calibration_mode

    @calibration_mode.setter
    def calibration_mode(self, value: bool) -> None:
        self._calibration_mode = bool(value)
        self.panel.calibrating = bool(value)

    def settings(self) -> dict:
        return {"pictureMode": self.picture_mode, "whiteBalanceMethod": "22", "whiteBalanceIre": "100",
                "backlight": self.backlight, **{k: list(v) for k, v in self.wb.items()}}

    def set_backlight(self, value) -> None:
        # OLED pixel brightness scales the whole light output.
        self.backlight = int(value)
        self.panel.peak = self.base_peak * self.backlight / self.USER_BACKLIGHT

    def neutral(self) -> None:
        self.wb = {k: [0] * 26 for k in self.wb}
        self.panel.upload([identity(i) for _ in range(3) for i in range(1024)])

    def handle(self, request: dict) -> dict:
        action = request.get("action") or ""
        self.requests.append(action)
        ok = {"status": "ok", "ip": request.get("ip") or "192.0.2.10", "client_key": "sim-key",
              "transport": "wss", "port": 3001, **self.MODEL}
        mode = request.get("picture_mode") or self.picture_mode
        if action in ("probe", "connect"):
            return ok
        if action == "calibration_mode":
            self.calibration_mode = bool(request.get("enable"))
            return {**ok, "calibration_mode": self.calibration_mode, "calibration_picture_mode": mode,
                    "active_picture_mode": mode, "message": "LG calibration mode "
                    + ("enabled" if self.calibration_mode else "disabled")}
        if action == "picture_get":
            return {**ok, "picture_settings": self.settings(), "active_picture_mode": mode}
        if action == "picture_set":
            settings = request.get("settings") or {}
            if settings.get("pictureMode"):
                self.picture_mode = settings["pictureMode"]
            if "backlight" in settings:
                self.set_backlight(settings["backlight"])
            if request.get("reset_ddc_baseline"):
                self.neutral()
                return {**ok, "picture_settings": self.settings(), "ddc_1d_lut": True,
                        "ddc_baseline_reset": True, "ddc_reset_verified": True, "calibration_mode": True,
                        "calibration_picture_mode": mode}
            for key in self.wb:
                if isinstance(settings.get(key), list):
                    self.wb[key] = list(settings[key])
            if request.get("keep_calibration_mode"):
                self.calibration_mode = True
            return {**ok, "settings": self.settings(), "picture_settings": self.settings(),
                    "readback": self.settings(), "ddc_1d_lut": True, "active_picture_mode": mode,
                    "calibration_picture_mode": mode}
        if action == "1d_dpg_upload":
            data = request.get("dpg_data")
            if not isinstance(data, list) or len(data) != 3072:
                return {"status": "error", "message": "bad dpg_data"}
            self.panel.upload(data)
            self.uploads += 1
            self.calibration_mode = bool(request.get("keep_calibration_mode"))
            return {**ok, "uploaded": True, "ddc_1d_lut": True, "message": "1D DPG uploaded",
                    "cal_start_response": {"type": "response"}, "cal_end_response": {"type": "response"},
                    "active_picture_mode": mode, "calibration_picture_mode": mode}
        if action == "picture_reset":
            self.picture_mode = mode
            self.neutral()
            self.set_backlight(self.FACTORY_BACKLIGHT)
            return {**ok, "picture_settings": self.settings(), "message": "picture mode reset"}
        if action == "hdr_calman_reset":
            self.neutral()
            self.panel.gamut = M_NATIVE
            self.panel.lut = None
            self.panel.tone_peak = self.base_peak          # factory tone map
            self.calibration_mode = False
            return {**ok, "hdr_calman_reset": True, "ddc_1d_lut": True, "ddc_baseline_reset": True,
                    "ddc_reset_verified": True, "message": "HDR reference reset"}
        if action == "hdr_tone_map_upload":
            data = request.get("dpg_data")
            if isinstance(data, list) and len(data) == 3072:
                self.panel.upload(data)
                self.uploads += 1
            self.panel.tone_peak = float(request.get("peak_luminance") or self.base_peak)
            self.tone_maps += 1
            self.calibration_mode = False                  # the tone map always ends with CAL_END
            return {**ok, "uploaded": True, "message": "HDR tone map uploaded",
                    "cal_start_response": {"type": "response"}, "cal_end_response": {"type": "response"},
                    "active_picture_mode": mode, "calibration_picture_mode": mode}
        if action == "sdr_calman_reset":
            self.neutral()
            self.panel.gamut = M_NATIVE      # identity 3x3: the factory BT.709 mapping is gone
            self.panel.lut = None
            self.calibration_mode = False
            return {**ok, "message": "SDR reference reset"}
        lut_ok = {"upload_command": "BT709_3D_LUT_DATA", "get_command": "GET_3D_LUT_DATA",
                  "upload_verified": True, "upload_supported": True,
                  "cal_start_response": {"type": "response"}, "cal_end_response": {"type": "response"}}
        if action == "3d_lut_reset":
            self.panel.lut = None
            self.calibration_mode = bool(request.get("keep_calibration_mode"))
            return {**ok, **lut_ok, "reset_to_unity": True, "message": "LG 3D LUT reset to unity and verified."}
        if action == "3d_lut_probe":
            return {**ok, **lut_ok, "message": "3D LUT upload supported"}
        if action == "3d_lut_upload":
            try:
                self.panel.lut = Lut3D.read(request.get("payload_path") or "")
            except (OSError, ValueError) as exc:
                return {"status": "error", "message": f"bad 3D LUT payload: {exc}"}
            self.lut_uploads += 1
            self.calibration_mode = bool(request.get("keep_calibration_mode"))
            return {**ok, **lut_ok, "message": "LG 3D LUT uploaded and verified."}
        return {"status": "error", "message": f"simulated TV does not implement {action}"}


def serve(tv: SimTV) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            result = json.dumps(tv.handle(json.loads(body or b"{}"))).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server

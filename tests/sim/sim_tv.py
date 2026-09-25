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


class Panel:
    def __init__(self, peak: float = 150.0, gains=(1.0, 0.95, 1.08), gammas=(2.32, 2.20, 2.40),
                 black_level: str = "high", gpu_range: str = "full"):
        self.peak = peak
        self.black_level = black_level
        self.gpu_range = gpu_range
        self.gains = gains
        self.gammas = gammas
        self.dpg = [[identity(i) for i in range(1024)] for _ in range(3)]
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
            for channel, code in enumerate(rgb):
                index = sample_index(code, self.black_level, self.gpu_range)
                drive = max(0.0, self.table_value(channel, index) / self.reference)
                light.append(self.gains[channel] * drive ** self.gammas[channel])
        return tuple(self.peak * sum(M709[row][c] * light[c] for c in range(3)) for row in range(3))

    def upload(self, data: list[int]) -> None:
        with self.lock:
            self.dpg = [[float(v) for v in data[c * 1024:(c + 1) * 1024]] for c in range(3)]


class SimPattern:
    def __init__(self):
        self.rgb = (0, 0, 0)
        self.shown = 0

    def show(self, r: int, g: int, b: int, area: float) -> None:
        self.rgb = (r, g, b)
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
        X, Y, Z = self.panel.xyz(self.pattern.rgb)
        # A colorimeter's repeatability is mostly common to all three
        # channels (luminance); the chromaticity is much steadier.
        common = 1 + self.random.gauss(0, 0.003)
        noise = lambda v: v * common * (1 + self.random.gauss(0, 0.0005)) + self.random.gauss(0, 0.0003)
        return noise(X), noise(Y), noise(Z)

    def restart(self) -> None:
        pass

    def close(self) -> None:
        pass


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
        if action == "sdr_calman_reset":
            self.neutral()
            self.calibration_mode = False
            return {**ok, "message": "SDR reference reset"}
        if action == "3d_lut_reset":
            return {**ok, "reset_to_unity": True, "message": "3D LUT reset"}
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

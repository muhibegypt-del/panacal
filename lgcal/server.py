"""Local stand-in for the PGenerator web API the AutoCal worker talks to.

On the Pi the worker calls the PGenerator daemon on 127.0.0.1:80. Here the
same routes are served on 127.0.0.1 by this process: patterns go to the
Windows pattern window, readings come from a persistent spotread, and the
LG routes run the author's pgenerator-lg helper.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .meter import read_with_recovery, xyz_record
from .patterns import to_8bit, window_area


class MeterService:
    """Mirrors meter_session.sh: draw the patch, wait delay_ms, read."""

    def __init__(self, pattern, meter, log, synthetic_black: bool = True):
        self.pattern = pattern
        self.meter = meter
        self.log = log
        self.synthetic_black = synthetic_black
        self.pattern_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.state: dict = {"status": "idle"}
        self.busy = False
        self.delay_scale = 1.0

    def show(self, r: int, g: int, b: int, input_max: int, size: int) -> None:
        with self.pattern_lock:
            self.pattern.show(to_8bit(r, input_max), to_8bit(g, input_max), to_8bit(b, input_max),
                              window_area(size))

    def pattern_route(self, payload: dict) -> dict:
        name = payload.get("name") or "patch"
        if name == "stop":
            self.show(0, 0, 0, 255, 100)
            return {"status": "ok", "pattern": "stop"}
        if name not in ("patch", "stabilization"):
            return {"status": "error", "message": f"Unknown pattern: {name}"}
        size = int(payload.get("size", 100) or 100)
        self.show(int(payload.get("r") or 0), int(payload.get("g") or 0), int(payload.get("b") or 0),
                  int(payload.get("input_max") or 255), size)
        return {"status": "ok", "pattern": "patch"}

    def start_read(self, payload: dict) -> dict:
        with self.state_lock:
            if self.busy:
                return {"status": "error", "message": "A meter reading is already in progress"}
            self.busy = True
            self.state = {"status": "measuring", "request_id": payload.get("request_id") or ""}
        threading.Thread(target=self._read, args=(payload,), daemon=True).start()
        return {"status": "measuring", "request_id": payload.get("request_id") or ""}

    def _read(self, payload: dict) -> None:
        request_id = payload.get("request_id") or ""
        try:
            r, g, b = (int(payload.get(key) or 0) for key in ("patch_r", "patch_g", "patch_b"))
            input_max = int(payload.get("input_max") or 255)
            self.show(r, g, b, input_max, int(payload.get("patch_size") or 10))
            delay = max(0, int(payload.get("delay_ms") or 0)) / 1000 * self.delay_scale
            if delay:
                time.sleep(delay)
            low_light = payload.get("low_light") if isinstance(payload.get("low_light"), dict) else {}
            count = int(low_light.get("requested_sample_count") or 1)
            if count not in (1, 2, 3, 5):
                raise ValueError("Invalid requested_sample_count")
            ire = payload.get("ire")
            is_black = float(ire) <= 0 if isinstance(ire, (int, float)) else r == g == b == 0
            if self.synthetic_black and r == g == b and is_black:
                # meter_session.sh: the 0% step on an emissive panel is
                # reported as 0 instead of reading meter noise (by IRE, so
                # it also covers limited-range black, code 16).
                record = {"X": 0, "Y": 0, "Z": 0, "x": 0, "y": 0, "luminance": 0.0, "cct": 0,
                          "sample_count": 0, "synthetic_black": True}
            else:
                samples = [read_with_recovery(self.meter, self.log) for _ in range(count)]
                xyz = tuple(sum(sample[i] for sample in samples) / count for i in range(3))
                record = xyz_record(xyz)
                record["sample_count"] = count
            record.update({
                "timestamp": int(time.time()), "requested_sample_count": count, "average_mode": "off",
                "request_id": request_id, "name": payload.get("name") or "", "ire": payload.get("ire"),
                "r_code": r, "g_code": g, "b_code": b,
            })
            self.log(f"READ {record['name'] or '?':>6} code {r},{g},{b}/{input_max}  "
                     f"Y={record['Y']:.4f} x={record['x']:.4f} y={record['y']:.4f}")
            state = {"status": "ok", "request_id": request_id, "readings": [record], "count": 1}
        except Exception as exc:  # reported to the worker, which decides
            self.log(f"READ failed: {exc}")
            state = {"status": "error", "request_id": request_id, "message": str(exc)}
        with self.state_lock:
            self.state = state
            self.busy = False

    def result(self) -> dict:
        with self.state_lock:
            return dict(self.state)


class Api:
    def __init__(self, meter_service: MeterService, lg, log):
        self.meter = meter_service
        self.lg = lg
        self.log = log

    def handle(self, method: str, path: str, payload: dict) -> dict:
        path = path.split("?", 1)[0]
        routes = {
            ("POST", "/api/pattern"): self.meter.pattern_route,
            ("POST", "/api/meter/read"): self.meter.start_read,
            ("GET", "/api/meter/read/result"): lambda _p: self.meter.result(),
            ("POST", "/api/meter/session/stop"): lambda _p: {"status": "ok"},
            ("GET", "/api/lg/status"): lambda _p: self.lg.status(),
            ("POST", "/api/lg/calibration-mode"): self.lg.calibration_mode,
            ("POST", "/api/lg/picture-settings"): self.lg.picture_settings,
            ("GET", "/api/lg/picture-settings"): self.lg.picture_settings,
            ("POST", "/api/lg/picture-settings/set"): self.lg.picture_settings_set,
            ("POST", "/api/lg/1d-dpg/upload"): self.lg.dpg_upload,
            ("POST", "/api/lg/3d-lut/reset"): self.lg.lut3d_reset,
        }
        handler = routes.get((method, path))
        if handler is None:
            self.log(f"API {method} {path}: not supported by the PC port")
            return {"status": "error", "message": f"{method} {path} is not supported by the PC port"}
        from .lg import redact
        result = handler(payload)
        return redact(result) if path.startswith("/api/lg/") else result


def make_server(api: Api, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def _serve(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw) if raw.strip() else {}
                if not isinstance(payload, dict):
                    payload = {}
            except ValueError:
                payload = {}
            try:
                result = api.handle(method, self.path, payload)
            except Exception as exc:
                api.log(f"API {method} {self.path} failed: {exc!r}")
                result = {"status": "error", "message": str(exc)}
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server

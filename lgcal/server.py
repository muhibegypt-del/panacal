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


def is_synthetic_black(payload: dict, enabled: bool) -> bool:
    """meter_session.sh: the 0% step on an emissive panel is reported as 0
    instead of reading meter noise. Decided by IRE, so it also covers
    limited-range black (code 16)."""
    if not enabled:
        return False
    r, g, b = (int(payload.get(key) or 0) for key in ("patch_r", "patch_g", "patch_b"))
    if not r == g == b:
        return False
    ire = payload.get("ire")
    return float(ire) <= 0 if isinstance(ire, (int, float)) and not isinstance(ire, bool) else r == 0


class MeterService:
    """Mirrors meter_session.sh: draw the patch, wait delay_ms, read.

    Reads run one at a time. If the worker asks again while a read is still
    running (after its own timeout), the new request waits its turn, and a
    finished read is only published if it is still the latest request, so a
    stale result can never answer a newer one."""

    def __init__(self, pattern, meter, log, synthetic_black: bool = True):
        self.pattern = pattern
        self.meter = meter
        self.log = log
        self.synthetic_black = synthetic_black
        self.pattern_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.read_lock = threading.Lock()
        self.state: dict = {"status": "idle"}
        self.latest = None
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
        request_id = str(payload.get("request_id") or "")
        token = object()
        with self.state_lock:
            self.latest = token
            self.state = {"status": "measuring", "request_id": request_id}
        threading.Thread(target=self._read, args=(payload, token), daemon=True).start()
        return {"status": "measuring", "request_id": request_id}

    def _read(self, payload: dict, token) -> None:
        with self.read_lock:
            with self.state_lock:
                if self.latest is not token:
                    return          # superseded before it started
            state = self._measure(payload)
            with self.state_lock:
                if self.latest is token:
                    self.state = state

    def _measure(self, payload: dict) -> dict:
        request_id = str(payload.get("request_id") or "")
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
            if is_synthetic_black(payload, self.synthetic_black):
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
            return {"status": "ok", "request_id": request_id, "readings": [record], "count": 1}
        except Exception as exc:  # reported to the worker (it retries or stops) and logged
            self.log(f"READ failed: {exc!r}")
            return {"status": "error", "request_id": request_id, "message": f"Meter read failed: {exc}"}

    def result(self) -> dict:
        with self.state_lock:
            return dict(self.state)


class Api:
    def __init__(self, meter_service: MeterService, lg, log, final_dpg=None):
        """final_dpg(table) -> table rewrites the LUT on the worker's
        final commit only (see lgcal.dark)."""
        self.meter = meter_service
        self.lg = lg
        self.log = log
        self.final_dpg = final_dpg

    def dpg_upload(self, payload: dict) -> dict:
        from .dark import is_final_commit
        if self.final_dpg is not None and is_final_commit(payload) and isinstance(payload.get("dpg_data"), list):
            payload = {**payload, "dpg_data": self.final_dpg(payload["dpg_data"])}
        return self.lg.dpg_upload(payload)

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
            ("POST", "/api/lg/1d-dpg/upload"): self.dpg_upload,
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
            try:
                length = max(0, int(self.headers.get("Content-Length") or 0))
                raw = self.rfile.read(length) if length else b""
                payload = json.loads(raw) if raw.strip() else {}
                if not isinstance(payload, dict):
                    raise ValueError("request body must be a JSON object")
            except ValueError as exc:
                api.log(f"API {method} {self.path}: bad request ({exc})")
                result = {"status": "error", "message": f"Bad request: {exc}"}
            else:
                try:
                    result = api.handle(method, self.path, payload)
                except Exception as exc:  # answered to the worker and logged
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

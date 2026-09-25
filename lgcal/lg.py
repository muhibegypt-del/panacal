"""The /api/lg/* routes the AutoCal worker calls.

A port of the matching handlers in PGenerator-Plus usr/share/PGenerator/lg.pm.
The TV conversation itself is still the author's pgenerator-lg helper; this
module only keeps the paired-TV store (clients.json) and builds the helper
requests exactly as lg.pm does.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IPV4_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$")


def valid_ipv4(ip) -> bool:
    match = IPV4_RE.match(str(ip or ""))
    return bool(match) and all(0 <= int(octet) <= 255 for octet in match.groups())


def helper_timeout(request: dict) -> int:
    """lg.pm lg_helper_timeout."""
    override = int(request.get("helper_timeout") or 0)
    if override > 0:
        return override
    action = request.get("action") or ""
    if action == "picture_set":
        settings = request.get("settings")
        if isinstance(settings, dict) and any(
                isinstance(settings.get(key), list)
                for key in ("whiteBalanceRed", "whiteBalanceGreen", "whiteBalanceBlue")):
            return 150
        return 45
    if action in ("3d_lut_probe", "3d_lut_upload", "3d_lut_reset"):
        return 180
    if action == "picture_reset":
        return 130
    if action == "picture_apply_all_inputs":
        return 60
    if action in ("calibration_mode", "hdr_tone_map_upload", "hdr_calman_reset", "1d_dpg_read"):
        return 75
    if action == "1d_dpg_upload":
        return 80
    if action == "picture_get":
        return 60
    return 90


TIMEOUT_WHAT = {
    "3d_lut_probe": "3D LUT command", "3d_lut_upload": "3D LUT command",
    "3d_lut_reset": "3D LUT command", "1d_dpg_upload": "1D DPG upload",
    "1d_dpg_read": "1D DPG readback", "picture_get": "picture-settings request",
    "calibration_mode": "calibration-mode change", "hdr_tone_map_upload": "HDR tone-map upload",
    "picture_reset": "picture-mode reset",
}


def helper_timeout_message(request: dict, timeout: int) -> str:
    action = request.get("action") or ""
    if action == "picture_set":
        settings = request.get("settings") or {}
        if list(settings) == ["pictureMode"]:
            return f"LG TV did not finish the picture-mode change within {timeout}s."
        return f"LG TV did not finish the white-balance write within {timeout}s."
    if action in TIMEOUT_WHAT:
        return f"LG TV did not finish the {TIMEOUT_WHAT[action]} within {timeout}s."
    return f"LG TV command timed out after {timeout}s."


def redact(value):
    """lg.pm lg_public_api_json: never hand the pairing key to API callers."""
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()
                if key not in ("client_key", "client-key", "clientKey")}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class LG:
    def __init__(self, perl: str, helper: Path, data_dir: Path, log, env: dict | None = None):
        self.perl = perl
        self.helper = helper
        self.data_dir = data_dir
        self.log = log
        self.env = env or {}
        self.gate = threading.Lock()
        data_dir.mkdir(parents=True, exist_ok=True)

    # --- clients.json -----------------------------------------------------
    @property
    def clients_file(self) -> Path:
        return self.data_dir / "clients.json"

    def load_clients(self) -> dict:
        try:
            data = json.loads(self.clients_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_clients(self, clients: dict) -> bool:
        temporary = self.clients_file.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(clients), encoding="utf-8")
            os.replace(temporary, self.clients_file)
            return True
        except OSError:
            return False

    @staticmethod
    def primary_client(clients: dict) -> dict:
        if any(clients.get(key) for key in ("client_key", "ip", "model_name", "name")):
            return clients
        for key in ("devices", "clients"):
            for entry in clients.get(key) or []:
                if isinstance(entry, dict) and (entry.get("client_key") or entry.get("client-key") or entry.get("ip")):
                    return entry
        return {}

    def client_key(self, clients: dict) -> str:
        client = self.primary_client(clients)
        return client.get("client_key") or client.get("client-key") or ""

    @staticmethod
    def target_ip(payload: dict, clients: dict) -> str:
        for ip in (payload.get("ip"), clients.get("ip"), clients.get("manual_ip")):
            if valid_ipv4(ip):
                return ip
        return ""

    def disconnected(self, clients: dict) -> bool:
        return bool(clients.get("disconnected")) and self.client_key(clients) != ""

    @staticmethod
    def device_identity(source: dict) -> tuple[str, str]:
        hello = source.get("hello_info") if isinstance(source.get("hello_info"), dict) else {}
        software = source.get("software_info") if isinstance(source.get("software_info"), dict) else {}
        uuid = hello.get("deviceUUID") or source.get("deviceUUID") or source.get("uuid") or ""
        mac = software.get("device_id") or source.get("device_id") or source.get("mac") or ""
        return str(uuid).lower(), str(mac).lower()

    def keyring_upsert(self, clients: dict, fields: dict) -> None:
        uuid, mac = fields.get("uuid", ""), fields.get("mac", "")
        if (not uuid and not mac) or not fields.get("client_key"):
            return
        devices = clients.setdefault("devices", [])
        entry = next((e for e in devices if isinstance(e, dict) and uuid and str(e.get("uuid", "")).lower() == uuid), None)
        if entry is None:
            entry = next((e for e in devices if isinstance(e, dict) and mac and str(e.get("mac", "")).lower() == mac), None)
        if entry is None:
            entry = {}
            devices.append(entry)
        if uuid:
            entry["uuid"] = uuid
        if mac:
            entry["mac"] = mac
        for key in ("client_key", "name", "model_name", "software_version"):
            if fields.get(key):
                entry[key] = fields[key]
        if fields.get("ip"):
            entry["last_ip"] = fields["ip"]
        entry["last_seen"] = int(time.time())

    def update_connect_metadata(self, result, manual_ip: str = "") -> dict:
        """lg.pm lg_update_connect_metadata."""
        clients = self.load_clients()
        if manual_ip:
            clients["manual_ip"] = manual_ip
        if not isinstance(result, dict):
            self.save_clients(clients)
            return clients
        if result.get("status") == "ok":
            ip = result.get("ip") or manual_ip or ""
            if ip:
                clients["ip"] = ip
            for key in ("client_key", "name", "model_name", "software_version", "transport"):
                if result.get(key):
                    clients[key] = result[key]
            for key in ("hello_info", "system_info", "software_info"):
                if isinstance(result.get(key), dict):
                    clients[key] = result[key]
            clients["last_seen"] = int(time.time())
            uuid, mac = self.device_identity(result)
            self.keyring_upsert(clients, {
                "uuid": uuid, "mac": mac, "client_key": result.get("client_key") or "",
                "name": result.get("name") or "", "model_name": result.get("model_name") or "",
                "software_version": result.get("software_version") or "", "ip": ip,
            })
            for key in ("disconnected", "disconnected_at", "last_error"):
                clients.pop(key, None)
        else:
            clients["last_error"] = result.get("message") or "LG connection failed"
        self.save_clients(clients)
        return clients

    def store_calibration_mode_state(self, clients: dict, active: bool, picture_mode: str) -> bool:
        stored_mode = clients.get("calibration_picture_mode") or ""
        if bool(clients.get("calibration_mode")) == bool(active) and (
                not active or not picture_mode or picture_mode == stored_mode):
            return True
        clients["calibration_mode"] = bool(active)
        if active:
            if picture_mode:
                clients["calibration_picture_mode"] = picture_mode
        else:
            clients.pop("calibration_picture_mode", None)
        return self.save_clients(clients)

    def prepare_held_calibration_mode(self, clients, keep, already_active, picture_mode):
        if not keep or already_active:
            return None
        if self.store_calibration_mode_state(clients, True, picture_mode or ""):
            return None
        return {"status": "error", "error_code": "lg-calibration-state-not-persisted",
                "message": "Unable to record the LG calibration session before starting the TV write."}

    def record_calibration_mode_result(self, clients, result, active, fallback_mode):
        if not isinstance(result, dict):
            return result
        if result.get("error_code") == "lg-calibration-end-unconfirmed":
            mode = result.get("calibration_picture_mode") or result.get("active_picture_mode") or fallback_mode or ""
            self.store_calibration_mode_state(clients, True, mode)
            result["calibration_mode"] = True
            result["calibration_session_unconfirmed"] = True
            if mode:
                result["calibration_picture_mode"] = mode
            return result
        if result.get("status") != "ok":
            return result
        if not active and result.get("calibration_session_unconfirmed"):
            return result
        mode = result.get("calibration_picture_mode") or result.get("active_picture_mode") or fallback_mode or ""
        self.store_calibration_mode_state(clients, active, mode)
        result["calibration_mode"] = bool(active)
        if active and mode:
            result["calibration_picture_mode"] = mode
        return result

    @staticmethod
    def needs_repair(result: dict) -> bool:
        if result.get("error_code") == "lg-calibration-permission":
            return False
        if result.get("ddc_1d_lut") and re.search(r"CAL_START returned 401", result.get("message") or "", re.I):
            return False
        if result.get("needs_repair") or result.get("error_code") == "insufficient-permissions":
            return True
        return bool(re.search(r"insufficient permissions", result.get("message") or "", re.I))

    # --- helper -----------------------------------------------------------
    def helper_env(self, request: dict | None = None) -> dict:
        env = os.environ.copy()
        env.update(self.env)
        if request is not None:
            # Compact JSON: Windows caps one environment variable at 32,767
            # characters, and a 1D LUT upload is about 25,000 encoded.
            env["PGEN_LG_REQUEST_B64"] = base64.b64encode(
                json.dumps(request, separators=(",", ":")).encode("utf-8")).decode("ascii")
        return env

    def run_helper(self, request: dict) -> dict:
        """lg.pm lg_helper_run: one TV conversation at a time."""
        with self.gate:
            timeout = helper_timeout(request)
            request["helper_timeout"] = timeout
            action = request.get("action")
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    [self.perl, str(self.helper)], env=self.helper_env(request),
                    capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
            except subprocess.TimeoutExpired:
                self.log(f"LG {action}: timed out after {timeout}s")
                return {"status": "error", "message": helper_timeout_message(request, timeout)}
            stdout = completed.stdout.decode("utf-8", errors="replace")
            stderr = completed.stderr.decode("utf-8", errors="replace")
            try:
                result = json.loads(stdout)
            except ValueError:
                result = None
            elapsed = time.monotonic() - started
            if isinstance(result, dict) and result.get("status"):
                self.log(f"LG {action}: {result.get('status')} in {elapsed:.1f}s"
                         + (f" ({result.get('message')})" if result.get("message") else ""))
                return result
            raw = re.sub(r"\s+", " ", (stdout + " " + stderr)).strip() or "LG helper execution failed"
            self.log(f"LG {action}: helper failed: {raw[:500]}")
            return {"status": "error", "message": raw}

    def _ready(self, payload: dict, what: str):
        clients = self.load_clients()
        not_connected = {"status": "error", "message": f"Connect the LG TV before {what}."}
        if self.disconnected(clients):
            return None, not_connected
        ip = self.target_ip(payload, clients)
        key = self.client_key(clients)
        if not ip or not key:
            return None, not_connected
        return (clients, ip, key), None

    # --- routes -----------------------------------------------------------
    def status(self) -> dict:
        clients = self.load_clients()
        key = self.client_key(clients)
        # The Pi reads TV power over HDMI-CEC; a PC cannot. The worker blocks
        # only a definite "off", so report the TV as on and let the first
        # authenticated helper call be the real check (as the worker does
        # when CEC is unknown).
        return {"status": "ok", "tv_power": "on", "paired": bool(key), "client_key_present": bool(key),
                "ip": clients.get("ip") or clients.get("manual_ip") or "",
                "model_name": clients.get("model_name") or "", "name": clients.get("name") or "",
                "calibration_mode": bool(clients.get("calibration_mode"))}

    def calibration_mode(self, payload: dict) -> dict:
        enabled = bool(payload.get("enabled"))
        ready, error = self._ready(payload, "changing calibration mode")
        if error:
            return error
        clients, ip, key = ready
        result = self.run_helper({
            "action": "calibration_mode", "ip": ip, "client_key": key, "enable": 1 if enabled else 0,
            "picture_mode": payload.get("picture_mode") or "", "signal_mode": payload.get("signal_mode") or "",
            "connect_timeout": 5,
        })
        if result.get("status") == "ok":
            clients = self.update_connect_metadata(result, clients.get("manual_ip") or ip)
            clients["calibration_mode"] = enabled
            if enabled:
                clients["calibration_picture_mode"] = (result.get("calibration_picture_mode")
                                                       or result.get("active_picture_mode") or "")
            else:
                clients.pop("calibration_picture_mode", None)
            self.save_clients(clients)
        response = dict(self.status())
        response.update({k: v for k, v in result.items() if k not in ("status", "message")})
        response["status"] = result.get("status") or "error"
        response["message"] = result.get("message") or "Unable to change LG calibration mode."
        return response

    def picture_settings(self, payload: dict) -> dict:
        ready, error = self._ready(payload, "reading picture settings")
        if error:
            return error
        clients, ip, key = ready
        keys = payload.get("keys")
        if not isinstance(keys, list) or not keys:
            keys = DEFAULT_PICTURE_KEYS
        picture_mode = payload.get("picture_mode") or ""
        if not picture_mode and not payload.get("ignore_calibration_picture_mode"):
            picture_mode = clients.get("calibration_picture_mode") or ""
        result = self.run_helper({
            "action": "picture_get", "ip": ip, "client_key": key, "keys": keys,
            "picture_mode": picture_mode, "signal_mode": payload.get("signal_mode") or "",
            "tv_input": "",
            "include_current_input": bool(payload.get("include_current_input")),
            "force_ddc_white_balance": bool(payload.get("force_ddc_white_balance")),
            "helper_timeout": int(payload.get("helper_timeout") or 0),
            "connect_timeout": 5,
        })
        if result.get("status") == "ok":
            self.update_connect_metadata(result, clients.get("manual_ip") or ip)
        return result

    def picture_settings_set(self, payload: dict) -> dict:
        ready, error = self._ready(payload, "changing picture settings")
        if error:
            return error
        clients, ip, key = ready
        settings = payload.get("settings")
        if not isinstance(settings, dict) or not settings:
            return {"status": "error", "message": "No LG picture settings were provided."}
        readback_keys = payload.get("readback_keys")
        if payload.get("skip_readback"):
            readback_keys = []
        elif not isinstance(readback_keys, list) or not readback_keys:
            readback_keys = list(settings)
        picture_mode = payload.get("picture_mode") or ""
        if not picture_mode and not payload.get("ignore_calibration_picture_mode"):
            picture_mode = clients.get("calibration_picture_mode") or ""
        ddc_white_balance = (settings.get("whiteBalanceMethod") == "22" and all(
            isinstance(settings.get(k), list) for k in ("whiteBalanceRed", "whiteBalanceGreen", "whiteBalanceBlue")))
        if "keep_calibration_mode" in payload:
            keep = bool(payload.get("keep_calibration_mode"))
        else:
            keep = bool(clients.get("calibration_mode") or ddc_white_balance)
        active = bool(payload.get("calibration_mode_active")
                      or (ddc_white_balance and keep and clients.get("calibration_mode")))
        reset_baseline = bool(payload.get("reset_ddc_baseline") or payload.get("clear_ddc_baseline"))
        if reset_baseline:
            active = False
        if ddc_white_balance:
            held = self.prepare_held_calibration_mode(clients, keep, active, picture_mode)
            if held:
                return held
        result = self.run_helper({
            "action": "picture_set", "ip": ip, "client_key": key, "settings": settings,
            "readback_keys": readback_keys, "picture_mode": picture_mode,
            "signal_mode": payload.get("signal_mode") or "", "tv_input": "",
            "keep_calibration_mode": 1 if keep else 0,
            "calibration_mode_active": 1 if active else 0,
            "reset_ddc_baseline": reset_baseline,
            "verify_ddc_upload": bool(payload.get("verify_ddc_upload")),
            "force_ddc_white_balance": bool(payload.get("force_ddc_white_balance")),
            "helper_timeout": int(payload.get("helper_timeout") or 0),
            "connect_timeout": 5,
        })
        # (The Pi restarts its renderer after a lone picture-mode write; a
        # Windows window does not lose its surface, so nothing to do here.)
        updated = clients
        if result.get("status") == "ok":
            updated = self.update_connect_metadata(result, clients.get("manual_ip") or ip)
        if result.get("status") == "ok" and ddc_white_balance and (result.get("ddc_1d_lut") or "calibration_mode" in result):
            updated["calibration_mode"] = keep
            mode = (result.get("calibration_picture_mode") or result.get("active_picture_mode")
                    or payload.get("picture_mode") or clients.get("calibration_picture_mode") or "")
            if keep:
                if mode:
                    updated["calibration_picture_mode"] = mode
            else:
                updated.pop("calibration_picture_mode", None)
            self.save_clients(updated)
            result["calibration_mode"] = keep
            if mode:
                result["calibration_picture_mode"] = mode
        if self.needs_repair(result):
            result["message"] = ("The saved LG client key does not have picture-control permission. "
                                 "Pair again with 'Pair LG TV.bat' and enter the PIN shown on the TV.")
        elif result.get("error_code") == "lg-calibration-permission":
            result["repair_hint"] = ("The TV accepted pairing but denied calibration access. Remove the "
                                     "existing LG Connect Apps entry on the TV, then pair again.")
        return result

    def _held_upload(self, payload: dict, action: str, what: str, extra: dict) -> dict:
        ready, error = self._ready(payload, what)
        if error:
            return error
        clients, ip, key = ready
        picture_mode = payload.get("picture_mode") or clients.get("calibration_picture_mode") or ""
        held = self.prepare_held_calibration_mode(
            clients, payload.get("keep_calibration_mode"), payload.get("calibration_mode_active"), picture_mode)
        if held:
            return held
        request = {"action": action, "ip": ip, "client_key": key, "picture_mode": picture_mode,
                   "signal_mode": payload.get("signal_mode") or "",
                   "helper_timeout": int(payload.get("helper_timeout") or 0), "connect_timeout": 5,
                   "keep_calibration_mode": 1 if payload.get("keep_calibration_mode") else 0,
                   "calibration_mode_active": 1 if payload.get("calibration_mode_active") else 0}
        request.update(extra)
        result = self.run_helper(request)
        self.record_calibration_mode_result(clients, result, bool(payload.get("keep_calibration_mode")), picture_mode)
        if result.get("status") == "ok":
            self.update_connect_metadata(result, clients.get("manual_ip") or ip)
        return result

    def dpg_upload(self, payload: dict) -> dict:
        data = payload.get("dpg_data")
        if data is None:
            return {"status": "error", "message": "1D DPG upload requires dpg_data."}
        if not isinstance(data, list) or len(data) != 3072:
            return {"status": "error", "expected_count": 3072,
                    "received_count": len(data) if isinstance(data, list) else -1,
                    "message": "1D DPG upload requires a 3072-value (3 channels x 1024 points) uint16 array."}
        normalized = [max(0, min(65535, int(float(v)))) for v in data]
        result = self._held_upload(payload, "1d_dpg_upload", "uploading the 1D DPG", {"dpg_data": normalized})
        if self.needs_repair(result):
            result["message"] = ("The saved LG client key does not have calibration permission. "
                                 "Pair again with 'Pair LG TV.bat', then rerun AutoCal.")
        return result

    def lut3d_reset(self, payload: dict) -> dict:
        return self._held_upload(payload, "3d_lut_reset", "resetting the 3D LUT", {
            "upload_command": payload.get("upload_command") or "",
            "get_command": payload.get("get_command") or "",
        })

    def clear_stale_calibration_mode(self) -> dict | None:
        """lg.pm lg_clear_stale_calibration_mode_for_reset: a run that died
        (power cut, closed window) leaves calibration mode held on the TV.
        Close it before the next run starts."""
        clients = self.load_clients()
        if not clients.get("calibration_mode"):
            return None
        result = self.calibration_mode({"enabled": False,
                                        "picture_mode": clients.get("calibration_picture_mode") or ""})
        if result.get("status") != "ok":
            self.log("LG stale calibration mode could not be closed: " + (result.get("message") or ""))
        return result

    def current_picture_mode(self) -> str:
        """The picture mode the TV is showing on this input right now."""
        result = self.picture_settings({"keys": ["pictureMode"], "ignore_calibration_picture_mode": True})
        settings = result.get("picture_settings") if isinstance(result.get("picture_settings"), dict) else {}
        return str(settings.get("pictureMode") or "")

    # --- pairing (used by the launcher) ------------------------------------
    def probe(self, ip: str) -> dict:
        return self.run_helper({"action": "probe", "ip": ip, "connect_timeout": 5})

    def connect(self, ip: str) -> dict:
        """lg.pm webui_lg_connect with a known IP."""
        clients = self.load_clients()
        clients["manual_ip"] = ip
        self.save_clients(clients)
        key = ""
        identified = False
        probe = self.probe(ip)
        if probe.get("status") == "ok":
            uuid, mac = self.device_identity(probe)
            identified = bool(uuid or mac)
            for entry in clients.get("devices") or []:
                if (uuid and str(entry.get("uuid", "")).lower() == uuid) or (mac and str(entry.get("mac", "")).lower() == mac):
                    key = entry.get("client_key") or ""
                    break
        if not key and not identified:
            key = self.client_key(clients)
        result = self.run_helper({"action": "connect", "ip": ip, "client_key": key,
                                  "connect_timeout": 5, "pair_timeout": 55})
        self.update_connect_metadata(result, ip)
        return result

    def start_pin_pairing(self, ip: str, session_dir: Path) -> tuple[subprocess.Popen, Path, Path]:
        """Start lg.pm's connect_pin_wait helper; the TV then shows a PIN."""
        session_dir.mkdir(parents=True, exist_ok=True)
        state_file = session_dir / "state.json"
        pin_file = session_dir / "pin.txt"
        for path in (state_file, pin_file):
            path.unlink(missing_ok=True)
        request = {"action": "connect_pin_wait", "ip": ip, "client_key": "", "pairing_type": "PIN",
                   "connect_timeout": 5, "pair_timeout": 55, "pin_wait_timeout": 150,
                   # Forward slashes: the helper splits directories on "/".
                   "state_file": state_file.as_posix(), "pin_file": pin_file.as_posix(),
                   "token": str(int(time.time()))}
        log = (session_dir / "helper.log").open("w", encoding="utf-8")
        process = subprocess.Popen([self.perl, str(self.helper)], env=self.helper_env(request),
                                   stdout=log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        log.close()
        return process, state_file, pin_file


DEFAULT_PICTURE_KEYS = [
    "pictureMode", "whiteBalanceMethod", "whiteBalancePoint", "whiteBalanceIre",
    "whiteBalanceIre10pt", "whiteBalanceCodeValue", "whiteBalanceCodeValue10pt",
    "whiteBalanceLuminance", "whiteBalanceColorTemperature", "colorTemperature",
    "whiteBalanceRed", "whiteBalanceGreen", "whiteBalanceBlue", "whiteBalanceRed10pt",
    "whiteBalanceGreen10pt", "whiteBalanceBlue10pt", "whiteBalanceRedGain",
    "whiteBalanceGreenGain", "whiteBalanceBlueGain", "whiteBalanceRedOffset",
    "whiteBalanceGreenOffset", "whiteBalanceBlueOffset", "oledLight", "backlight",
    "adjustingLuminance", "adjustingLuminance10pt",
]

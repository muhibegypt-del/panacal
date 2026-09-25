"""LG AutoCal on a Windows PC: pairing, launch and progress.

The calibration itself is PGenerator-Plus's meter_lg_autocal.pl, run
unmodified apart from the PC-PORT lines listed in PATCHES.md.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .lg import LG, valid_ipv4
from .meter import Meter, read_with_recovery, xyz_record
from .patterns import PatternWindow, window_area
from .server import Api, MeterService, make_server
from .steps import build_config

ROOT = Path(__file__).resolve().parents[1]
PGEN = ROOT / "pgen"
WORKER = PGEN / "bin" / "meter_lg_autocal.pl"
HELPER = PGEN / "bin" / "pgenerator-lg"
SETTINGS = ROOT / "settings.json"
DATA_DIR = ROOT / "data" / "lg"
SESSIONS = ROOT / "sessions"


class Log:
    def __init__(self, path: Path | None = None, echo: bool = False):
        self.file = path.open("a", encoding="utf-8") if path else None
        self.echo = echo
        self.lock = threading.Lock()

    def __call__(self, message: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}] {message}"
        with self.lock:
            if self.file:
                self.file.write(line + "\n")
                self.file.flush()
            if self.echo:
                print(line, flush=True)

    def close(self) -> None:
        if self.file:
            self.file.close()


def say(message: str = "") -> None:
    print(message, flush=True)


def load_settings(path: Path = SETTINGS) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        example = ROOT / "settings.example.json"
        if path == SETTINGS and example.is_file():
            shutil.copyfile(example, path)
            raise SystemExit(f"Created {path}. Set tv_ip and the Perl/Argyll paths in it, then run again.")
        raise SystemExit(f"Missing {path}.")
    except ValueError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}")


def find_perl(settings: dict) -> str:
    perl = settings.get("perl") or shutil.which("perl") or ""
    if not perl or not (Path(perl).is_file() or shutil.which(perl)):
        raise SystemExit("Perl was not found. Install Strawberry Perl (strawberryperl.com) or set "
                         "\"perl\" in settings.json.")
    check = subprocess.run([perl, "-MIO::Socket::SSL", "-MJSON::PP", "-MDigest::SHA", "-e", "print 1"],
                           capture_output=True, text=True)
    if check.stdout.strip() != "1":
        raise SystemExit("Perl is missing IO::Socket::SSL, JSON::PP or Digest::SHA "
                         "(Strawberry Perl includes all three):\n" + check.stderr.strip())
    return perl


def perl_env(session_dir: Path | None, port: int | None, helper: Path = HELPER) -> dict:
    """Environment for the worker and helper (see PATCHES.md)."""
    env = {
        "PGEN_LG_HELPER": helper.as_posix(),
        "PGEN_LG_DATA_DIR": DATA_DIR.as_posix(),
        "PGEN_LG_NATIVE_TLS": "1",
        "PERL5LIB": (PGEN / "share" / "PGenerator").as_posix(),
    }
    if session_dir is not None:
        env["PGEN_LG_TMP_DIR"] = session_dir.as_posix()
    if port is not None:
        env["PGEN_API_PORT"] = str(port)
    return env


def meter_command(settings: dict) -> list[str]:
    meter = settings.get("meter") or {}
    spotread = meter.get("spotread") or ""
    if not spotread:
        argyll = settings.get("argyll_bin") or ""
        spotread = str(Path(argyll) / "spotread.exe") if argyll else (shutil.which("spotread") or "")
    if not spotread or not Path(spotread).is_file():
        raise SystemExit(f"spotread was not found ({spotread or 'not set'}). Set meter.spotread in settings.json.")
    command = [spotread, *[str(arg) for arg in meter.get("args", [])]]
    ccss = meter.get("ccss") or ""
    if ccss:
        if not Path(ccss).is_file():
            raise SystemExit(f"meter.ccss does not exist: {ccss}")
        command += ["-X", ccss]
    return command


# --- pairing ----------------------------------------------------------------

def pair(settings: dict, ip: str | None = None, pin_input=input) -> int:
    ip = ip or settings.get("tv_ip") or ""
    if not valid_ipv4(ip):
        raise SystemExit("Set tv_ip in settings.json to the LG TV's IPv4 address "
                         "(TV Settings > General > Network > Wired/Wi-Fi > Advanced).")
    perl = find_perl(settings)
    log = Log(echo=False)
    lg = LG(perl, HELPER, DATA_DIR, log, perl_env(None, None))
    say(f"Contacting the LG TV at {ip} ...")
    probe = lg.probe(ip)
    if probe.get("status") != "ok":
        say("No LG webOS TV answered: " + (probe.get("message") or "unknown error"))
        say("Check the IP, that the TV is on, and Settings > General > External Devices > "
            "TV On With Mobile / LG Connect Apps is enabled.")
        return 1
    say(f"Found {probe.get('model_name') or 'LG TV'} ({probe.get('software_version') or 'webOS'}) "
        f"over {probe.get('transport')}:{probe.get('port')}")
    say("If the TV asks whether to allow the connection, accept it.")
    result = lg.connect(ip)
    if result.get("status") == "ok" and result.get("client_key") and not result.get("pair_prompted"):
        say("Already paired: " + (result.get("message") or "connected"))
        return 0
    if result.get("status") == "ok" and result.get("client_key"):
        say("Paired: " + (result.get("message") or "connected"))
        return 0
    say("PIN pairing is needed so the TV grants calibration access.")
    process, state_file, pin_file = lg.start_pin_pairing(ip, DATA_DIR / "pin-session")
    state = wait_pin_state(state_file, process, ("pending", "ok", "error"), 30)
    if state.get("status") == "pending":
        pin = ""
        while not pin.isdigit() or not 4 <= len(pin) <= 8:
            pin = pin_input("Enter the PIN shown on the TV: ").strip()
        temporary = pin_file.with_suffix(".tmp")
        temporary.write_text(pin + "\n", encoding="utf-8")
        os.replace(temporary, pin_file)
        state = wait_pin_state(state_file, process, ("ok", "error"), 90)
    process.wait(timeout=30)
    if state.get("status") == "ok":
        lg.update_connect_metadata(state, ip)
        say(f"Paired with {state.get('model_name') or 'the LG TV'}. The key is saved in {DATA_DIR}.")
        return 0
    say("Pairing failed: " + (state.get("message") or "no answer from the TV"))
    return 1


def wait_pin_state(path: Path, process, wanted: tuple, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("status") in wanted:
                return state
        except (OSError, ValueError):
            pass
        if process.poll() is not None and not path.exists():
            break
        time.sleep(0.25)
    return {"status": "error", "message": "The TV did not answer the pairing request in time."}


# --- run --------------------------------------------------------------------

def run(settings: dict, *, pattern=None, meter=None, helper: Path = HELPER, perl: str | None = None,
        interactive: bool = True, session_dir: Path | None = None, extra_env: dict | None = None,
        config_overrides: dict | None = None, delay_scale: float = 1.0) -> int:
    """Run one AutoCal. The keyword hooks exist for the offline simulation."""
    perl = perl or find_perl(settings)
    session_dir = session_dir or SESSIONS / datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir.mkdir(parents=True, exist_ok=True)
    log = Log(session_dir / "autocal.log", echo=False)
    port = int(settings.get("api_port") or 8765)
    env = perl_env(session_dir, port, helper)
    env.update(extra_env or {})
    lg = LG(perl, helper, DATA_DIR, log, env)
    clients = lg.load_clients()
    if not lg.client_key(clients):
        say("The LG TV is not paired yet. Run 'Pair LG TV.bat' first.")
        return 1
    say(f"LG TV: {clients.get('model_name') or 'paired TV'} at {clients.get('ip') or clients.get('manual_ip')}")
    owned = []
    server = None
    worker = None
    try:
        if pattern is None:
            pattern_settings = settings.get("pattern") or {}
            pattern = PatternWindow(session_dir, log, pattern_settings.get("screen", ""))
            owned.append(pattern)
            pattern.start()
            dispwin = Path(settings.get("argyll_bin") or "") / "dispwin.exe"
            if pattern_settings.get("linearize_video_lut", True) and dispwin.is_file():
                pattern.linearize_video_lut(dispwin)
        if meter is None:
            say("Starting the meter ...")
            meter = Meter(meter_command(settings), log)
            owned.append(meter)
        synthetic_black = bool((settings.get("meter") or {}).get("synthetic_black", True))
        service = MeterService(pattern, meter, log, synthetic_black)
        service.delay_scale = delay_scale
        server = make_server(Api(service, lg, log), port)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        # The dashboard's setup step: measure the current 100% white; it is
        # the luminance reference the worker preserves.
        size = int(settings.get("patch_size") or 10)
        pattern.show(255, 255, 255, window_area(size))
        if interactive:
            input("Place the meter on the centre of the white patch on the TV, then press Enter ...")
        time.sleep(2.0 * delay_scale)
        white = xyz_record(read_with_recovery(meter, log))
        say(f"Current white: {white['Y']:.1f} cd/m2  x={white['x']:.4f} y={white['y']:.4f}")
        config = build_config(settings, white["Y"])
        config.update(config_overrides or {})
        config_file = session_dir / "worker_config.json"
        state_file = session_dir / "worker_state.json"
        stop_file = session_dir / "worker.stop"
        config_file.write_text(json.dumps(config, indent=1), encoding="utf-8")
        state_file.write_text(json.dumps({"status": "running", "autocal": True, "current_step": 0,
                                          "total_steps": 0, "current_name": "Starting LG Auto Cal...",
                                          "message": "Starting", "readings": []}), encoding="utf-8")
        worker_log = (session_dir / "worker.log").open("w", encoding="utf-8")
        full_env = os.environ.copy()
        full_env.update(env)
        worker = subprocess.Popen(
            # Forward slashes: the worker finds its modules by splitting its
            # own path on "/".
            [perl, WORKER.as_posix(), config_file.as_posix(), state_file.as_posix(), stop_file.as_posix()],
            env=full_env, stdout=worker_log, stderr=subprocess.STDOUT, cwd=str(session_dir),
            # Ctrl+C must not reach the worker or a helper mid-write to the
            # TV; stopping goes through the stop file, as on the Pi.
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        say(f"AutoCal running. Session folder: {session_dir}")
        say("Press Ctrl+C to stop; the TV is left in a clean state either way.")
        final = monitor(worker, state_file, stop_file)
        worker_log.close()
        return report(final, session_dir)
    except KeyboardInterrupt:
        say("Stopped.")
        return 1
    finally:
        if worker is not None and worker.poll() is None:
            worker.kill()
        if server is not None:
            server.shutdown()
            server.server_close()
        for resource in reversed(owned):
            try:
                resource.close()
            except Exception as exc:
                log(f"close failed: {exc}")
        log.close()


def read_state(path: Path) -> dict:
    for _ in range(5):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            time.sleep(0.05)
    return {}


def final_state(path: Path) -> dict:
    """The worker writes state.tmp then renames it over the state file. On
    Windows that rename fails if this process has the file open at that
    instant, leaving the newest state in the .tmp file."""
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists() and (not path.exists() or temporary.stat().st_mtime >= path.stat().st_mtime):
        state = read_state(temporary)
        if state:
            return state
    return read_state(path)


def monitor(worker: subprocess.Popen, state_file: Path, stop_file: Path) -> dict:
    last = None
    stopping = False
    while True:
        try:
            done = worker.poll() is not None
            state = read_state(state_file)
            line = progress_line(state)
            if line and line != last:
                say(line)
                last = line
            if done:
                return final_state(state_file)
            time.sleep(1.0)
        except KeyboardInterrupt:
            if stopping:
                raise
            stopping = True
            say("Stopping: the worker is ending the TV calibration session (up to 2 minutes) ...")
            stop_file.write_text("stop\n", encoding="utf-8")
            try:
                worker.wait(timeout=150)
            except subprocess.TimeoutExpired:
                pass


def progress_line(state: dict) -> str:
    if not state:
        return ""
    step = state.get("current_step") or 0
    total = state.get("total_steps") or 0
    name = state.get("current_name") or ""
    message = state.get("message") or ""
    where = f"{step}/{total} " if total else ""
    return f"[{datetime.now():%H:%M:%S}] {where}{name} | {message}".rstrip(" |")


def report(state: dict, session_dir: Path) -> int:
    status = state.get("status") or "unknown"
    say("")
    say(f"Result: {status.upper()} - {state.get('message') or ''}")
    history = state.get("sdr_1d_dpg_anchor_history")
    if isinstance(history, dict) and history:
        rows = []
        for label, entries in history.items():
            if not isinstance(entries, list) or not entries:
                continue
            try:
                ire = float(label.split("_", 1)[-1].split("%", 1)[0])
            except ValueError:
                continue
            best = min(entries, key=lambda e: float(e.get("de") or 1e9))
            rows.append((ire, label, best))
        rows.sort(key=lambda row: -row[0])
        say(f"{'Level':>13}  {'Y cd/m2':>9}  {'target':>9}  {'dE ITP':>6}")
        for ire, label, best in rows:
            say(f"{label.removeprefix('sdr26_'):>13}  {float(best.get('measured_Y') or 0):9.3f}  "
                f"{float(best.get('target_Y') or 0):9.3f}  {float(best.get('de') or 0):6.2f}")
    final_de = state.get("sdr_1d_dpg_final_de")
    if isinstance(final_de, (int, float)):
        say(f"Worst level after calibration: dE ITP {final_de:.2f} "
            f"(target {state.get('sdr_1d_dpg_target_de') or state.get('target_delta_e')})")
    if state.get("sdr_1d_dpg_single_socket_commit"):
        say("The 1D LUT is committed to the TV and calibration mode is closed.")
    say(f"Logs and the worker state are in {session_dir}")
    return 0 if status == "complete" else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="LG OLED AutoCal on a PC (PGenerator-Plus worker)")
    sub = parser.add_subparsers(dest="command", required=True)
    pair_parser = sub.add_parser("pair", help="pair with the LG TV (PIN)")
    pair_parser.add_argument("--ip")
    sub.add_parser("run", help="run the SDR greyscale AutoCal")
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.command == "pair":
        return pair(settings, args.ip)
    return run(settings)

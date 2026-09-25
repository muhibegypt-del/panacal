"""LG AutoCal on a Windows PC, in one click.

Finds (or fetches) Perl and ArgyllCMS, finds the TV on the network, pairs
only if needed, prepares the Windows display, waits for the meter, measures
which video range the TV expects, runs PGenerator-Plus's AutoCal worker
(meter_lg_autocal.pl, see PATCHES.md), verifies the result, and puts
Windows back as it was.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .discover import discover
from .display import DisplaySetup
from .lg import LG
from .meter import Meter
from .patterns import PatternWindow
from .server import Api, MeterService, make_server
from .setup import find_argyll, find_ccss, find_perl, meter_command
from .signal import Reader, detect_range, verify, wait_for_meter
from .steps import PICTURE_MODES, TARGET_GAMMAS, build_config

ROOT = Path(__file__).resolve().parents[1]
PGEN = ROOT / "pgen"
WORKER = PGEN / "bin" / "meter_lg_autocal.pl"
HELPER = PGEN / "bin" / "pgenerator-lg"
SETTINGS = ROOT / "settings.json"
DATA_DIR = ROOT / "data" / "lg"
SESSIONS = ROOT / "sessions"
MODE_NAMES = {"expert1": "Expert (Bright Room)", "expert2": "Expert (Dark Room)", "filmMaker": "Filmmaker",
              "cinema": "Cinema", "game": "Game Optimizer", "normal": "Standard", "eco": "Eco",
              "sports": "Sports", "vivid": "Vivid", "personalized": "Personalised"}
DEFAULTS = {"target_gamma": "bt1886", "target_delta_e": 0.5, "patch_size": 10, "api_port": 8765}


class Log:
    def __init__(self, path: Path | None = None):
        self.file = path.open("a", encoding="utf-8") if path else None
        self.lock = threading.Lock()

    def __call__(self, message: str) -> None:
        if self.file:
            with self.lock:
                self.file.write(f"[{datetime.now():%H:%M:%S}] {message}\n")
                self.file.flush()

    def close(self) -> None:
        if self.file:
            self.file.close()


def say(message: str = "") -> None:
    print(message, flush=True)


def load_settings(path: Path = SETTINGS) -> dict:
    """Optional overrides; everything works without the file."""
    settings = dict(DEFAULTS)
    if path.is_file():
        try:
            settings.update(json.loads(path.read_text(encoding="utf-8")))
        except ValueError as exc:
            raise SystemExit(f"{path} is not valid JSON: {exc}")
    if str(settings["target_gamma"]).lower() not in TARGET_GAMMAS:
        raise SystemExit(f"target_gamma must be one of {', '.join(TARGET_GAMMAS)}")
    return settings


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


# --- the TV -----------------------------------------------------------------

def find_tv(lg: LG, settings: dict, pin_input=input) -> str:
    clients = lg.load_clients()
    known = settings.get("tv_ip") or clients.get("ip") or clients.get("manual_ip") or ""
    tvs = discover(lg.probe, known, say)
    if not tvs:
        raise SystemExit("No LG TV answered on the network. Check that the TV is on, on the same network as "
                         "this PC, and that LG Connect Apps is enabled in the TV's network settings.")
    tv = tvs[0]
    if len(tvs) > 1:
        paired_uuid = str((clients.get("hello_info") or {}).get("deviceUUID") or "").lower()
        match = [t for t in tvs if str((t.get("hello_info") or {}).get("deviceUUID") or "").lower() == paired_uuid]
        if match and paired_uuid:
            tv = match[0]
        else:
            for number, item in enumerate(tvs, 1):
                say(f"  {number}. {item.get('model_name') or 'LG TV'} at {item['ip']}")
            choice = pin_input("More than one LG TV found. Type the number of the one to calibrate: ").strip()
            tv = tvs[int(choice) - 1] if choice.isdigit() and 0 < int(choice) <= len(tvs) else tvs[0]
    say(f"TV: {tv.get('model_name') or 'LG TV'} at {tv['ip']}")
    ensure_paired(lg, tv["ip"], pin_input)
    return tv["ip"]


def ensure_paired(lg: LG, ip: str, pin_input=input) -> None:
    result = lg.connect(ip)
    if result.get("status") == "ok" and result.get("client_key"):
        return
    say("Pairing with the TV (first time only). If the TV asks to allow the connection, accept it.")
    process, state_file, pin_file = lg.start_pin_pairing(ip, DATA_DIR / "pin-session")
    state = wait_pin_state(state_file, process, ("pending", "ok", "error"), 30)
    if state.get("status") == "pending":
        pin = ""
        while not pin.isdigit() or not 4 <= len(pin) <= 8:
            pin = pin_input("Type the PIN shown on the TV and press Enter: ").strip()
        temporary = pin_file.with_suffix(".tmp")
        temporary.write_text(pin + "\n", encoding="utf-8")
        os.replace(temporary, pin_file)
        state = wait_pin_state(state_file, process, ("ok", "error"), 90)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
    if state.get("status") != "ok":
        raise SystemExit("Pairing failed: " + (state.get("message") or "no answer from the TV"))
    lg.update_connect_metadata(state, ip)
    say("Paired. The TV will not ask again.")


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


def choose_picture_mode(lg: LG, settings: dict) -> str:
    mode = settings.get("picture_mode") or lg.current_picture_mode()
    if mode in PICTURE_MODES:
        say(f"Calibrating the picture mode the TV is in: {MODE_NAMES.get(mode, mode)}")
        return mode
    say(f"The TV is in a mode AutoCal cannot calibrate ({mode or 'unknown'}); "
        "calibrating Expert (Dark Room) instead.")
    return "expert2"


# --- run --------------------------------------------------------------------

def run(settings: dict, *, pattern=None, meter=None, helper: Path = HELPER, perl: str | None = None,
        session_dir: Path | None = None, extra_env: dict | None = None, config_overrides: dict | None = None,
        delay_scale: float = 1.0, find_tv_on_network: bool = True, pin_input=input) -> int:
    """One complete AutoCal. The keyword hooks exist for the offline simulation."""
    session_dir = session_dir or SESSIONS / datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir.mkdir(parents=True, exist_ok=True)
    log = Log(session_dir / "autocal.log")
    perl = perl or find_perl(settings, say)
    port = int(settings.get("api_port") or 8765)
    env = perl_env(session_dir, port, helper)
    env.update(extra_env or {})
    lg = LG(perl, helper, DATA_DIR, log, env)
    owned = []
    server = None
    worker = None
    try:
        if find_tv_on_network:
            find_tv(lg, settings, pin_input)
        elif not lg.client_key(lg.load_clients()):
            raise SystemExit("The TV is not paired.")
        if lg.clear_stale_calibration_mode():
            say("Closed a calibration session left open by an earlier run.")
        picture_mode = choose_picture_mode(lg, settings)

        if pattern is None:
            argyll = find_argyll(settings, say)
            display = DisplaySetup(session_dir, log)
            owned.append(display)
            try:
                state = display.prepare()
                say(f"Patterns go to {state.get('name') or 'the TV'} ({state['device']})"
                    + ("; Windows switched to Extend for the run" if state.get("topology_changed") else "")
                    + ("; Windows HDR turned off for the run" if state.get("hdr_changed") else ""))
                if int(state.get("bits") or 8) > 8:
                    log(f"NOTE output is {state['bits']} bpc; 8-bit patterns are expanded by the GPU")
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
                log(f"DISPLAY automatic setup failed: {exc}")
                say("Could not set up the display automatically; using the second screen as it is.")
                state = {"device": ""}
            pattern = PatternWindow(session_dir, log, state["device"])
            owned.append(pattern)
            pattern.start()
            dispwin = argyll / "dispwin.exe"
            if dispwin.is_file():
                pattern.linearize_video_lut(dispwin)
        if meter is None:
            argyll = find_argyll(settings, say)
            ccss = find_ccss(settings)
            say(f"Meter correction: {Path(ccss).name}" if ccss else
                "No WOLED meter correction (.ccss) found; readings use the meter's default. "
                "Drop one into the 'ccss' folder to use it.")
            say("Starting the meter ...")
            meter = Meter(meter_command(settings, argyll, ccss), log)
            owned.append(meter)

        size = int(settings.get("patch_size") or 10)
        reader = Reader(pattern, meter, log, size, settle_scale=delay_scale)
        white = wait_for_meter(reader, say)
        limited = detect_range(reader, white["Y"], say)
        config = build_config(settings, white["Y"], limited=limited, picture_mode=picture_mode)
        config.update(config_overrides or {})

        service = MeterService(pattern, meter, log, bool((settings.get("meter") or {}).get("synthetic_black", True)))
        service.delay_scale = delay_scale
        server = make_server(Api(service, lg, log), port)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        config_file = session_dir / "worker_config.json"
        state_file = session_dir / "worker_state.json"
        stop_file = session_dir / "worker.stop"
        config_file.write_text(json.dumps(config, indent=1), encoding="utf-8")
        state_file.write_text(json.dumps({"status": "running", "autocal": True, "current_step": 0,
                                          "total_steps": 0, "current_name": "Starting",
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
        say("")
        say("Calibrating. Leave the meter where it is; Ctrl+C stops safely.")
        final = monitor(worker, state_file, stop_file)
        worker_log.close()
        ok = report(final)
        if ok:
            result = verify(reader, config["target_gamma"], limited, say)
            (session_dir / "verification.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        say(f"Everything from this run is in {session_dir}")
        return 0 if ok else 1
    except KeyboardInterrupt:
        say("Stopped.")
        return 1
    finally:
        if worker is not None and worker.poll() is None:
            worker.kill()
            # A killed worker cannot close calibration mode itself.
            lg.clear_stale_calibration_mode()
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
    started = time.monotonic()
    last = None
    stopping = False
    while True:
        try:
            done = worker.poll() is not None
            line = progress_line(read_state(state_file), time.monotonic() - started)
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
            say("Stopping: the worker is closing the TV's calibration session (up to 2 minutes) ...")
            stop_file.write_text("stop\n", encoding="utf-8")
            try:
                worker.wait(timeout=150)
            except subprocess.TimeoutExpired:
                pass


def progress_line(state: dict, elapsed: float) -> str:
    """One line per level, with a time estimate once a few levels are done."""
    name = str(state.get("current_name") or "")
    if not name:
        return ""
    step, total = int(state.get("current_step") or 0), int(state.get("total_steps") or 0)
    label = name.replace("SDR26 1D DPG ", "").replace("sdr26_", "")
    clock = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
    if total and 3 <= step < total:
        left = elapsed / step * (total - step)
        return f"[{clock}] {step}/{total} {label}  (about {max(1, round(left / 60))} min left)"
    return f"[{clock}] {f'{step}/{total} ' if total else ''}{label}"


def report(state: dict) -> bool:
    status = state.get("status") or "unknown"
    say("")
    if status != "complete":
        say(f"AutoCal did not finish: {state.get('message') or status}")
        return False
    final_de = state.get("sdr_1d_dpg_final_de")
    say("AutoCal finished" + (f": worst level during calibration dE ITP {final_de:.2f}"
                              if isinstance(final_de, (int, float)) else "") + ".")
    if state.get("sdr_1d_dpg_single_socket_commit"):
        say("The new 1D LUT is saved in the TV and calibration mode is closed.")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="LG OLED AutoCal on a PC (PGenerator-Plus worker)")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "pair"],
                        help="run (default) or pair: only find and pair the TV")
    args = parser.parse_args(argv)
    settings = load_settings()
    try:
        if args.command == "pair":
            perl = find_perl(settings, say)
            find_tv(LG(perl, HELPER, DATA_DIR, Log(), perl_env(None, None)), settings)
            return 0
        return run(settings)
    except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
        say("")
        say(f"Stopped: {exc}")
        say(f"Details are in the newest folder under {SESSIONS}")
        return 1

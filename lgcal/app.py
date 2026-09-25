"""LG AutoCal on a Windows PC, in one click.

Finds (or fetches) Perl and ArgyllCMS, finds the TV on the network, pairs
only if needed, prepares the TV and the Windows display, waits for the
meter, measures which video range the TV expects, runs PGenerator-Plus's
AutoCal worker (meter_lg_autocal.pl, see PATCHES.md), verifies the result,
and puts Windows back as it was.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import settings as settings_file
from .discover import discover
from .display import DisplaySetup
from .lg import LG
from .meter import Meter
from .patterns import PatternWindow
from .prepare import clear_calibration, prepare
from .server import Api, MeterService, make_server
from .setup import find_argyll, find_ccss, find_perl, meter_command
from .signal import Reader, detect_range, measure_white, verify, wait_for_meter
from .steps import PICTURE_MODES, build_config

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
DEFAULTS = settings_file.validate({})[0]
HEARTBEAT = 90          # seconds without a progress line before "still working"


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
            self.file = None


CONSOLE: list = []   # console.txt of the open session, if any


def say(message: str = "") -> None:
    print(message, flush=True)
    for handle in CONSOLE:
        handle.write(message + "\n")
        handle.flush()


def ask(prompt: str, answer=input) -> str:
    """Console input; a closed console stops the run with a reason."""
    try:
        reply = answer(prompt)
    except EOFError:
        raise SystemExit("No keyboard input available (the console was closed).") from None
    for handle in CONSOLE:
        handle.write(prompt + str(reply) + "\n")
    return str(reply).strip()


class Session:
    """One folder per run under sessions/: autocal.log (everything),
    console.txt (what the window showed). Always closed, whatever happens."""

    def __init__(self, directory: Path | None = None, suffix: str = ""):
        self.directory = directory or SESSIONS / (datetime.now().strftime("%Y%m%d_%H%M%S") + suffix)
        self.log = None
        self.console = None

    def __enter__(self) -> "Session":
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log = Log(self.directory / "autocal.log")
        self.console = (self.directory / "console.txt").open("a", encoding="utf-8")
        CONSOLE.append(self.console)
        return self

    def __exit__(self, *exc) -> None:
        if self.console in CONSOLE:
            CONSOLE.remove(self.console)
        self.console.close()
        self.log.close()


def free_port(preferred: int) -> int:
    """The preferred local API port, or any free one if it is taken."""
    import socket
    for port in (preferred, 0):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return sock.getsockname()[1]
            except OSError:
                continue
    return preferred


def keep_awake(on: bool) -> None:
    """Stop Windows blanking the TV or sleeping during a long run with no
    keyboard or mouse input (ES_CONTINUOUS | ES_SYSTEM_REQUIRED |
    ES_DISPLAY_REQUIRED), and clear it again afterwards."""
    if os.name != "nt":
        return
    import ctypes
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000003 if on else 0x80000000)


def console_click_proof() -> None:
    """Clicking a Windows console with QuickEdit on pauses the program until
    a key is pressed; turn QuickEdit off for this window. Purely cosmetic:
    if the console does not allow it, nothing else changes."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, (mode.value & ~0x0040) | 0x0080)
    except (OSError, AttributeError):
        pass


def load_settings(path: Path = SETTINGS) -> dict:
    """Optional overrides, validated before anything touches the TV."""
    try:
        settings, warnings = settings_file.load(path)
    except ValueError as exc:
        raise SystemExit(f"{exc}\nFix settings.json (or delete it to use the defaults) and run again.")
    for warning in warnings:
        say(f"Note: {warning}.")
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

def choose_number(prompt: str, count: int, answer=input) -> int:
    """Ask until a number 1..count is typed; returns a 0-based index."""
    while True:
        reply = ask(prompt, answer)
        if reply.isdigit() and 1 <= int(reply) <= count:
            return int(reply) - 1
        say(f"  Please type a number from 1 to {count}.")


def find_tv(lg: LG, settings: dict, pin_input=input) -> str:
    clients = lg.load_clients()
    known = settings.get("tv_ip") or clients.get("ip") or clients.get("manual_ip") or ""
    tvs = discover(lg.probe, known, say)
    if not tvs:
        raise SystemExit("No LG TV answered on the network. Check that the TV is on, on the same network as "
                         "this PC, and that LG Connect Apps is enabled in the TV's network settings. "
                         "If the network blocks discovery, put its address in settings.json as \"tv_ip\".")
    tv = tvs[0]
    if len(tvs) > 1:
        paired_uuid = str((clients.get("hello_info") or {}).get("deviceUUID") or "").lower()
        match = [t for t in tvs if str((t.get("hello_info") or {}).get("deviceUUID") or "").lower() == paired_uuid]
        if match and paired_uuid:
            tv = match[0]
        else:
            for number, item in enumerate(tvs, 1):
                say(f"  {number}. {item.get('model_name') or 'LG TV'} at {item['ip']}")
            tv = tvs[choose_number("More than one LG TV found. Type the number of the one to calibrate: ",
                                   len(tvs), pin_input)]
    say(f"TV: {tv.get('model_name') or 'LG TV'} at {tv['ip']}")
    ensure_paired(lg, tv["ip"], pin_input)
    return tv["ip"]


def ensure_paired(lg: LG, ip: str, pin_input=input) -> None:
    say("Connecting to the TV ...")
    result = lg.connect(ip)
    if result.get("status") == "ok" and result.get("client_key"):
        return
    say("Pairing with the TV (first time only). If the TV asks to allow the connection, accept it.")
    process, state_file, pin_file = lg.start_pin_pairing(ip, DATA_DIR / "pin-session")
    try:
        state = wait_pin_state(state_file, process, ("pending", "ok", "error"), 30)
        if state.get("status") == "pending":
            pin = ""
            while not (pin.isdigit() and 4 <= len(pin) <= 8):
                pin = ask("Type the PIN shown on the TV and press Enter: ", pin_input)
            temporary = pin_file.with_suffix(".tmp")
            temporary.write_text(pin + "\n", encoding="utf-8")
            os.replace(temporary, pin_file)
            state = wait_pin_state(state_file, process, ("ok", "error"), 90)
    finally:
        # The helper waits up to 150 s for a PIN; never leave it behind.
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for leftover in (pin_file, pin_file.with_suffix(".tmp")):
            leftover.unlink(missing_ok=True)
    if state.get("status") != "ok":
        raise SystemExit("Pairing failed: " + (state.get("message") or "no answer from the TV") +
                         ". Check the TV is on, then run again.")
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
            pass            # not written yet, or mid-write: read again shortly
        if process.poll() is not None and not path.exists():
            return {"status": "error", "message": "The pairing helper stopped before the TV answered"}
        time.sleep(0.25)
    return {"status": "error", "message": "The TV did not answer the pairing request in time"}


def choose_picture_mode(lg: LG, settings: dict, ask_input=input) -> str:
    mode = settings.get("picture_mode") or lg.current_picture_mode()
    if mode in PICTURE_MODES:
        say(f"Calibrating the picture mode the TV is in: {MODE_NAMES.get(mode, mode)}")
        return mode
    # Older sets (2021 and earlier) may not report their mode. Calibrating
    # a mode the TV is not showing would measure nothing useful, so ask.
    say(f"The TV did not report a picture mode AutoCal can calibrate ({mode or 'none reported'}).")
    choices = list(PICTURE_MODES)
    for number, key in enumerate(choices, 1):
        say(f"  {number}. {MODE_NAMES.get(key, key)}")
    return choices[choose_number("Which picture mode is the TV showing? Type its number: ",
                                 len(choices), ask_input)]


# --- run --------------------------------------------------------------------

TV_STATE = {
    "untouched": "Nothing on the TV was changed.",
    "prepared": ("The picture mode was reset and its calibration cleared, so the TV now shows its factory "
                 "white balance. Run again to calibrate it."),
    "calibrating": ("The TV keeps the 1D LUT from the last finished step. Run again for a complete "
                    "calibration, or use 'Undo LG AutoCal.bat' to return it to factory."),
    "calibrated": "The calibration is saved in the TV.",
}


def run(settings: dict, *, pattern=None, meter=None, helper: Path = HELPER, perl: str | None = None,
        session_dir: Path | None = None, extra_env: dict | None = None, config_overrides: dict | None = None,
        delay_scale: float = 1.0, find_tv_on_network: bool = True, pin_input=input,
        reset_first: bool = True) -> int:
    """One complete AutoCal. Returns 0 on success, 1 on failure, 2 for a
    deliberate stop with a reason. The keyword hooks exist for the offline
    simulation."""
    with Session(session_dir) as session:
        tv_state = ["untouched"]
        keep_awake(True)
        try:
            code = _run(session, settings, tv_state, pattern=pattern, meter=meter, helper=helper, perl=perl,
                        extra_env=extra_env, config_overrides=config_overrides, delay_scale=delay_scale,
                        find_tv_on_network=find_tv_on_network, pin_input=pin_input, reset_first=reset_first)
        except KeyboardInterrupt:
            say("Stopped.")
            code = 1
        except SystemExit as exc:
            if isinstance(exc.code, int) or exc.code is None:
                raise
            say(str(exc.code))       # a deliberate stop with its reason
            code = 2
        except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
            session.log("STOPPED " + traceback.format_exc())
            say(f"Stopped: {exc}")
            code = 1
        except Exception as exc:
            session.log("UNEXPECTED " + traceback.format_exc())
            say(f"Stopped by an unexpected error ({exc!r}). The full error is in autocal.log; "
                "please send the session folder.")
            code = 1
        finally:
            keep_awake(False)
        say(TV_STATE[tv_state[0]])
        say(f"Everything from this run is in {session.directory}")
        return code


def _run(session: Session, settings: dict, tv_state: list, *, pattern, meter, helper, perl, extra_env,
         config_overrides, delay_scale, find_tv_on_network, pin_input, reset_first) -> int:
    log = session.log
    perl = perl or find_perl(settings, say)
    port = free_port(int(settings.get("api_port") or 8765))
    env = {**perl_env(session.directory, port, helper), **(extra_env or {})}
    lg = LG(perl, helper, DATA_DIR, log, env)
    owned = []
    server = None
    worker = None
    worker_log = None
    try:
        if find_tv_on_network:
            find_tv(lg, settings, pin_input)
        elif not lg.client_key(lg.load_clients()):
            raise SystemExit("The TV is not paired.")
        stale = lg.clear_stale_calibration_mode()
        if stale is not None:
            say("Closed a calibration session left open by an earlier run." if stale.get("status") == "ok" else
                "An earlier run left the TV in calibration mode and it could not be closed "
                f"({stale.get('message')}); continuing, the reset below closes it.")
        picture_mode = choose_picture_mode(lg, settings, pin_input)

        # Everything that can stop the run is checked before the TV is
        # touched: display, meter and the video-range chain.
        if pattern is None:
            argyll = find_argyll(settings, say)
            pattern = open_pattern_window(session, argyll, owned)
        if meter is None:
            argyll = find_argyll(settings, say)
            ccss = find_ccss(settings)
            say(f"Meter correction: {Path(ccss).name}" if ccss else
                "No WOLED meter correction (.ccss) found; readings use the meter's default. "
                "Drop one into the 'ccss' folder to use it.")
            say("Starting the meter ...")
            meter = Meter(meter_command(settings, argyll, ccss), log)
            owned.append(meter)

        reader = Reader(pattern, meter, log, int(settings.get("patch_size") or 10), settle_scale=delay_scale)
        white = wait_for_meter(reader, say)
        limited = detect_range(reader, white["Y"], say)
        if reset_first:
            tv_state[0] = "prepared"
            prepare(lg, picture_mode, MODE_NAMES.get(picture_mode, picture_mode), say,
                    sleep=lambda s: time.sleep(s * delay_scale),
                    factory_reset=bool(settings.get("reset_picture_mode", True)))
            # The reset changes white (contrast, gamma) and can change the
            # TV's Black Level, so both are measured again.
            white = measure_white(reader, say)
            before = limited
            limited = detect_range(reader, white["Y"], lambda _message: None)
            if limited != before:
                say("After the reset the TV expects " + ("limited" if limited else "full") +
                    "-range video; patterns follow it.")
        config = build_config(settings, white["Y"], limited=limited, picture_mode=picture_mode)
        config.update(config_overrides or {})

        meter_settings = settings.get("meter") or {}
        service = MeterService(pattern, meter, log, bool(meter_settings.get("synthetic_black", True)))
        service.delay_scale = delay_scale
        server = make_server(Api(service, lg, log), port)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        state_file = session.directory / "worker_state.json"
        stop_file = session.directory / "worker.stop"
        config_file = session.directory / "worker_config.json"
        config_file.write_text(json.dumps(config, indent=1), encoding="utf-8")
        state_file.write_text(json.dumps({"status": "running", "autocal": True, "current_step": 0,
                                          "total_steps": 0, "current_name": "Starting",
                                          "message": "Starting", "readings": []}), encoding="utf-8")
        worker_log = (session.directory / "worker.log").open("w", encoding="utf-8")
        tv_state[0] = "calibrating"
        worker = subprocess.Popen(
            # Forward slashes: the worker finds its modules by splitting its
            # own path on "/".
            [perl, WORKER.as_posix(), config_file.as_posix(), state_file.as_posix(), stop_file.as_posix()],
            env={**os.environ, **env}, stdout=worker_log, stderr=subprocess.STDOUT,
            cwd=str(session.directory),
            # Ctrl+C must not reach the worker or a helper mid-write to the
            # TV; stopping goes through the stop file, as on the Pi.
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        say("")
        say("Calibrating. Leave the meter where it is; Ctrl+C stops safely.")
        final = monitor(worker, state_file, stop_file)
        worker_log.close()
        ok = report(final, session.directory / "worker.log")
        if ok:
            tv_state[0] = "calibrated"
            result = verify(reader, config["target_gamma"], limited, say)
            (session.directory / "verification.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        return 0 if ok else 1
    finally:
        if worker is not None and worker.poll() is None:
            worker.kill()
            worker.wait(timeout=10)
            # A killed worker cannot close calibration mode itself.
            closed = lg.clear_stale_calibration_mode()
            if closed is not None and closed.get("status") != "ok":
                say("The TV may still be in calibration mode; switching it off and on clears it.")
        if worker_log is not None:
            worker_log.close()
        if server is not None:
            server.shutdown()
            server.server_close()
        for resource in reversed(owned):
            try:
                resource.close()
            except Exception as exc:  # keep closing the rest; report it
                log(f"close failed for {type(resource).__name__}: {exc!r}")
                say(f"Note: could not close the {type(resource).__name__} cleanly ({exc}).")


def open_pattern_window(session: Session, argyll: Path, owned: list) -> PatternWindow:
    """Windows display prepared (Extend, HDR off), pattern window on the TV,
    video LUT linear. Everything added to `owned` is undone on exit."""
    log = session.log
    display = DisplaySetup(session.directory, log, say=say)
    owned.append(display)
    try:
        state = display.prepare()
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        log(f"DISPLAY automatic setup failed: {exc}")
        say(f"Could not set up the display automatically ({str(exc).splitlines()[0][:200]}); "
            "using the second screen as it is.")
        state = {"device": ""}
    else:
        say(f"Patterns go to {state.get('name') or 'the TV'} ({state['device']})"
            + ("; Windows switched to Extend for the run" if state.get("topology_changed") else "")
            + ("; Windows HDR turned off for the run" if state.get("hdr_changed") else ""))
        if int(state.get("bits") or 8) > 8:
            log(f"NOTE output is {state['bits']} bpc; 8-bit patterns are expanded by the GPU")
        if state.get("night_light"):
            raise SystemExit("Windows Night light is on. It tints everything the PC sends, and the "
                             "calibration would bake that tint into the TV. Turn it off (Settings > "
                             "System > Display > Night light) and run again.")
    pattern = PatternWindow(session.directory, log, state["device"])
    owned.append(pattern)
    pattern.start()
    dispwin = argyll / "dispwin.exe"
    if dispwin.is_file():
        try:
            pattern.linearize_video_lut(dispwin)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            log(f"PATTERN video LUT not linearised: {exc}")
            say("Note: could not reset the PC's video LUT for the TV; if a colour profile is "
                "loaded for it, results may be off.")
    return pattern


def read_state(path: Path) -> dict:
    """The worker's state file; {} if it cannot be read after 5 tries (it is
    replaced atomically, so a failed read only means "try again")."""
    for _ in range(5):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
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


def monitor(worker: subprocess.Popen, state_file: Path, stop_file: Path, clock=time.monotonic) -> dict:
    started = clock()
    last, last_change = None, started
    stopping = False
    while True:
        try:
            done = worker.poll() is not None
            state = read_state(state_file)
            line = progress_line(state, clock() - started)
            if line and line != last:
                say(line)
                last, last_change = line, clock()
            elif clock() - last_change >= HEARTBEAT:
                last_change = clock()
                say(f"  ... still working ({state.get('message') or 'waiting for the meter or the TV'})")
            if done:
                return final_state(state_file)
            time.sleep(1.0)
        except KeyboardInterrupt:
            if stopping:
                raise
            stopping = True
            say("Stopping: the worker is closing the TV's calibration session (up to 2 minutes; "
                "press Ctrl+C again to force it) ...")
            stop_file.write_text("stop\n", encoding="utf-8")
            try:
                worker.wait(timeout=150)
            except subprocess.TimeoutExpired:
                say("The worker did not stop in time; forcing it.")


def progress_line(state: dict, elapsed: float) -> str:
    """One line per level, with a time estimate once a few levels are done."""
    name = str(state.get("current_name") or "")
    if not name:
        return ""
    try:
        step, total = int(state.get("current_step") or 0), int(state.get("total_steps") or 0)
    except (TypeError, ValueError):
        step, total = 0, 0
    label = name.replace("SDR26 1D DPG ", "").replace("sdr26_", "")
    clock = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
    if total > 0 and 3 <= step < total:
        left = elapsed / step * (total - step)
        return f"[{clock}] {step}/{total} {label}  (about {max(1, round(left / 60))} min left)"
    return f"[{clock}] {f'{step}/{total} ' if total > 0 else ''}{label}"


def tail(path: Path, lines: int = 6) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line for line in text if line.strip()][-lines:]


def report(state: dict, worker_log: Path) -> bool:
    status = state.get("status") or "unknown"
    say("")
    if status != "complete":
        say(f"AutoCal did not finish: {state.get('message') or 'the worker stopped without a status'}")
        last = tail(worker_log)
        if last:
            say("Last lines of worker.log:")
            for line in last:
                say("  " + line[:200])
        return False
    final_de = state.get("sdr_1d_dpg_final_de")
    say("AutoCal finished" + (f": worst level during calibration dE ITP {final_de:.2f}"
                              if isinstance(final_de, (int, float)) else "") + ".")
    if state.get("sdr_1d_dpg_single_socket_commit"):
        say("The new 1D LUT is saved in the TV and calibration mode is closed.")
    return True


def pair_or_undo(settings: dict, command: str) -> int:
    with Session(suffix="_" + command) as session:
        try:
            perl = find_perl(settings, say)
            lg = LG(perl, HELPER, DATA_DIR, session.log, perl_env(None, None))
            find_tv(lg, settings)
            if command == "undo":
                lg.clear_stale_calibration_mode()
                mode = choose_picture_mode(lg, settings)
                clear_calibration(lg, mode, say)
                say(f"{MODE_NAMES.get(mode, mode)} is back to the TV's factory white balance and LUTs.")
            return 0
        except KeyboardInterrupt:
            say("Stopped.")
        except SystemExit as exc:
            if isinstance(exc.code, int) or exc.code is None:
                raise
            say(str(exc.code))
        except Exception as exc:
            session.log("STOPPED " + traceback.format_exc())
            say(f"Stopped: {exc}")
        say(f"Details are in {session.directory}")
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="LG OLED AutoCal on a PC (PGenerator-Plus worker)")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "pair", "undo"],
                        help="run (default); pair: only find and pair the TV; "
                             "undo: return the TV's white balance and LUTs to factory")
    args = parser.parse_args(argv)
    console_click_proof()
    settings = load_settings()
    if args.command in ("pair", "undo"):
        return pair_or_undo(settings, args.command)
    return run(settings)

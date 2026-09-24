"""Proven hardware adapters: Panasonic ISFccc, DISPLAY2 patterns and Argyll."""
from __future__ import annotations

import json
import math
import os
import queue
import re
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from colour import median_xyz, xyz_to_uv, xyz_to_xyy
from domain import Measurement, XYZ


STX, ETX = b"\x02", b"\x03"
XYZ_RE = re.compile(r"Result is XYZ:\s*([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)")
CONTROL_LEVELS = tuple(range(10, 101, 10))
PICTURE_NUMBER_RANGES = {
    "PC:BRI": (-50, 50),
    "PC:CON": (0, 100),
    "PC:COL": (0, 100),
    "PC:TIN": (-50, 50),
    "PC:SHP": (0, 100),
}


def replace_with_retry(source: Path, target: Path, timeout: float = 2.0) -> None:
    """Atomically replace target, waiting out brief Windows sharing locks.

    The pattern host reads the command file every tick; while it has the
    file open Windows refuses the replace with PermissionError (WinError 5).
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def signed_value(value: int) -> str:
    value = int(value)
    return f"{value:03d}" if value >= 0 else f"-{abs(value):02d}"


class Transcript:
    def __init__(self, path: Path):
        self.path = path
        self.handle = path.open("w", encoding="utf-8", buffering=1)

    def write(self, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        print(line, flush=True)
        self.handle.write(line + "\n")

    def close(self) -> None:
        self.handle.close()


class PatternHost:
    def __init__(self, directory: Path, log: Transcript, config: dict):
        self.control = directory / "pattern_command.json"
        self.status = directory / "pattern_status.json"
        self.stderr_path = directory / "pattern_host_errors.log"
        self.stderr = self.stderr_path.open("w", encoding="utf-8")
        self.log = log
        self.config = config
        self.sequence = 0
        self.process: subprocess.Popen | None = None
        self.device: str | None = None
        self.dispwin: Path | None = None
        self.dispwin_index: int | None = None
        self.saved_lut: Path | None = None

    def _write_command(self, stimulus: int = 0, close: bool = False) -> None:
        self.sequence += 1
        data = {
            "sequence": self.sequence,
            "stimulus": int(stimulus),
            "range": self.config["pattern"]["range"],
            "close": bool(close),
            "window_area": float(self.config["pattern"]["window_area"]),
        }
        temporary = self.control.with_suffix(".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        replace_with_retry(temporary, self.control)

    def start(self, stimulus: int = 50) -> None:
        self._write_command(stimulus)
        self.process = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(Path(__file__).resolve().parent / "pattern_host.ps1"),
             "-ControlFile", str(self.control), "-StatusFile", str(self.status)],
            stdout=self.stderr,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        status = self._wait(self.sequence, 12.0)
        expected = self.config["pattern"]["display"]
        bounds = [status["left"], status["top"], status["width"], status["height"]]
        self.log.write(f"PATTERN HOST device={status['device']} primary={status['primary']} bounds={bounds}")
        # Windows may renumber displays and their desktop coordinates whenever a
        # monitor, cable or GPU output changes. The host has already required
        # exactly one non-primary screen; verify its stable properties here.
        expected_width = int(expected["width"])
        expected_height = int(expected["height"])
        if status["primary"] or bounds[2:] != [expected_width, expected_height]:
            raise RuntimeError(
                "Panasonic pattern display must be the sole 1920x1080 secondary "
                f"screen; detected {status}"
            )
        self.device = str(status["device"])

    @staticmethod
    def _dispwin_display_index(output: str, device: str) -> int:
        wanted = device.upper().removeprefix("\\\\.\\")
        for match in re.finditer(r"^\s*(\d+)\s*=\s*'([^,']+)", output, re.MULTILINE):
            if match.group(2).strip().upper() == wanted:
                return int(match.group(1))
        raise RuntimeError(f"dispwin could not map pattern display {device!r}")

    def linearize_video_lut(self, executable: Path) -> None:
        if not self.device:
            raise RuntimeError("Pattern display is not initialized")
        if not executable.is_file():
            raise RuntimeError(f"Required Argyll LUT utility is missing: {executable}")
        help_run = subprocess.run(
            [str(executable), "-?"], capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=15,
        )
        listing = (help_run.stdout or "") + "\n" + (help_run.stderr or "")
        index = self._dispwin_display_index(listing, self.device)
        saved = self.control.parent / "original_video_lut.cal"
        save_run = subprocess.run(
            [str(executable), "-d", str(index), "-s", str(saved)],
            capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=15,
        )
        if save_run.returncode != 0 or not saved.is_file():
            raise RuntimeError(
                "Could not save the Panasonic video LUT before clearing it: "
                + ((save_run.stdout or "") + (save_run.stderr or "")).strip()
            )
        self.dispwin = executable
        self.dispwin_index = index
        self.saved_lut = saved
        clear_run = subprocess.run(
            [str(executable), "-d", str(index), "-c"],
            capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=15,
        )
        if clear_run.returncode != 0:
            raise RuntimeError(
                "Could not load a linear video LUT on the Panasonic: "
                + ((clear_run.stdout or "") + (clear_run.stderr or "")).strip()
            )
        self.log.write(
            f"SIGNAL CHAIN video LUT linearized with dispwin display={index} device={self.device}"
        )

    def restore_video_lut(self) -> None:
        if not self.dispwin or self.dispwin_index is None or not self.saved_lut:
            return
        restored = subprocess.run(
            [str(self.dispwin), "-d", str(self.dispwin_index), str(self.saved_lut)],
            capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=15,
        )
        if restored.returncode != 0:
            raise RuntimeError(
                "Could not restore the original Panasonic video LUT: "
                + ((restored.stdout or "") + (restored.stderr or "")).strip()
            )
        self.log.write("SIGNAL CHAIN original video LUT restored after failure")
        self.dispwin = None
        self.dispwin_index = None
        self.saved_lut = None

    def show(self, stimulus: int) -> None:
        if not self.process or self.process.poll() is not None:
            raise RuntimeError("Pattern host is not running")
        self._write_command(stimulus)
        status = self._wait(self.sequence, 5.0)
        self.log.write(
            f"PATCH {stimulus:3d}% area={status['window_area']:.3f} "
            f"range={self.config['pattern']['range']} {status['device']}"
        )

    def _wait(self, sequence: int, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process and self.process.poll() is not None:
                raise RuntimeError("Pattern host exited: " + self.error_text())
            try:
                status = json.loads(self.status.read_text(encoding="utf-8-sig"))
                if status.get("ready") and status.get("sequence") == sequence:
                    return status
            except Exception as exc:
                last_error = exc
            time.sleep(0.005)
        raise RuntimeError(f"Pattern acknowledgement timed out ({last_error}); {self.error_text()}")

    def error_text(self) -> str:
        try:
            self.stderr.flush()
            return self.stderr_path.read_text(encoding="utf-8")[-1000:]
        except Exception:
            return ""

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                self._write_command(close=True)
                self.process.wait(timeout=3)
            except Exception:
                self.process.terminate()
        self.stderr.close()


class TVSession:
    VALUE_RE = re.compile(r"([-+]\d{2,3}|\d{3})$")

    def __init__(self, ip: str, log: Transcript, accepted_models: list[str], mode: str = "DAY"):
        self.log = log
        self.sock: socket.socket | None = None
        self.buffer = bytearray()
        try:
            self.sock = socket.create_connection((ip, 2048), timeout=5)
            self.sock.settimeout(5)
            self.command("QPW")
            model_reply = self.command("MDL")
            if not any(name.upper() in model_reply.upper() for name in accepted_models):
                raise RuntimeError(f"Unsupported TV model: {model_reply!r}")
            self.command("MIC:STA0713")
            self.command("VIC:" + mode)
            self.model = model_reply.split(":", 1)[-1]
        except Exception:
            if self.sock:
                self.sock.close()
                self.sock = None
            raise

    def command(self, body: str) -> str:
        if not self.sock:
            raise RuntimeError("TV socket is closed")
        self.log.write("TV > " + body)
        self.sock.sendall(STX + body.encode("ascii") + ETX)
        while ETX not in self.buffer:
            chunk = self.sock.recv(256)
            if not chunk:
                raise RuntimeError("TV closed the socket")
            self.buffer.extend(chunk)
        start, end = self.buffer.index(STX), self.buffer.index(ETX)
        reply = bytes(self.buffer[start + 1:end]).decode("ascii", "replace")
        del self.buffer[:end + 1]
        self.log.write("TV < " + reply)
        if reply.startswith("ER"):
            raise RuntimeError(f"TV rejected {body}: {reply}")
        return reply

    def get_number(self, code: str) -> int:
        reply = self.command("Q" + code)
        if not reply.startswith("Q" + code):
            raise RuntimeError(f"Unexpected reply for {code}: {reply!r}")
        match = self.VALUE_RE.fullmatch(reply[len("Q" + code):].strip())
        if not match:
            raise RuntimeError(f"Cannot parse numeric {code} reply: {reply!r}")
        return int(match.group(1))

    def set_number(self, code: str, value: int,
                   minimum: int = -50, maximum: int = 50) -> int:
        value = max(int(minimum), min(int(maximum), int(value)))
        self.command("V" + code + signed_value(value))
        actual = self.get_number(code)
        if actual != value:
            raise RuntimeError(f"{code} readback mismatch: requested {value}, got {actual}")
        return actual

    def get_text(self, code: str) -> str:
        reply = self.command("Q" + code)
        prefix = "Q" + code
        if not reply.startswith(prefix):
            raise RuntimeError(f"Cannot parse {code} reply: {reply!r}")
        return reply[len(prefix):]

    def set_text(self, code: str, value: str) -> str:
        self.command("V" + code + value)
        actual = self.get_text(code)
        if actual.casefold() != value.casefold():
            raise RuntimeError(f"{code} readback mismatch: requested {value!r}, got {actual!r}")
        return actual

    def apply_picture(self, required: dict) -> None:
        for code, value in required.items():
            if code in PICTURE_NUMBER_RANGES:
                low, high = PICTURE_NUMBER_RANGES[code]
                self.set_number(code, int(value), low, high)
            else:
                self.set_text(code, str(value))

    def select_point(self, level: int) -> None:
        if level not in CONTROL_LEVELS:
            raise ValueError(f"Invalid detailed-control level {level}")
        encoded = f"{level:03d}"
        self.command("VWB:ISL" + encoded)
        self.command("VWB:SLG" + encoded)

    def snapshot(self) -> dict:
        picture_codes = ("PC:BRI", "PC:CON", "PC:COL", "PC:TIN", "PC:SHP",
                         "PC:TMP", "PC:GMM", "PC:PBR", "PC:CGU")
        numeric_picture = {"PC:BRI", "PC:CON", "PC:COL", "PC:TIN", "PC:SHP"}
        picture = {
            code: self.get_number(code) if code in numeric_picture else self.get_text(code)
            for code in picture_codes
        }
        two_point = {
            code: self.get_number(code)
            for code in ("WB:HIR", "WB:HIG", "WB:HIB", "WB:LOR", "WB:LOG", "WB:LOB")
        }
        detail = {}
        for level in CONTROL_LEVELS:
            self.select_point(level)
            detail[str(level)] = {
                "WB:GNR": self.get_number("WB:GNR"),
                "WB:GNG": self.get_number("WB:GNG"),
                "WB:GNB": self.get_number("WB:GNB"),
                "PC:GGN": self.get_number("PC:GGN"),
            }
        return {"model": self.model, "picture": picture,
                "two_point": two_point, "detail": detail}

    def restore_calibration(self, snapshot: dict) -> None:
        for code, value in snapshot["two_point"].items():
            self.set_number(code, value)
        for level_text, values in snapshot["detail"].items():
            self.select_point(int(level_text))
            for code, value in values.items():
                self.set_number(code, value)

    def restore(self, snapshot: dict) -> dict:
        self.apply_picture(snapshot["picture"])
        self.restore_calibration(snapshot)
        verified = self.snapshot()
        for section in ("picture", "two_point", "detail"):
            if verified[section] != snapshot[section]:
                raise RuntimeError(f"Restore verification failed for {section}")
        return verified

    def close(self) -> None:
        if not self.sock:
            return
        try:
            self.command("VIC:FIX")
            self.log.write("TV > MIC:END")
            self.sock.sendall(STX + b"MIC:END" + ETX)
        except Exception as exc:
            self.log.write("TV CLOSE WARNING " + repr(exc))
        finally:
            self.sock.close()
            self.sock = None


class Meter:
    READY_PROMPT = "any other key to take a reading:"
    RETRY_PROMPT = "any other key to retry:"

    def __init__(self, executable: Path, args: list[str], log: Transcript,
                 *, startup_timeout: float = 25.0, read_timeout: float = 45.0):
        self.executable = executable
        self.args = args
        self.log = log
        self.startup_timeout = startup_timeout
        self.read_timeout = read_timeout
        self.process: subprocess.Popen | None = None
        self.lines: queue.Queue = queue.Queue()
        self.recent_output = deque(maxlen=12)
        self.reader_thread: threading.Thread | None = None
        self.ready = False
        try:
            self._start()
        except BaseException:
            # The caller cannot close an object whose constructor failed.
            self.close()
            raise

    @property
    def command(self) -> list[str]:
        return [str(self.executable), *self.args]

    def _reader(self, stream) -> None:
        pending = bytearray()
        try:
            # Argyll flushes prompts WITHOUT a newline. readline() deadlocks
            # here while spotread waits for our measurement trigger.
            while True:
                char = stream.read(1)
                if not char:
                    break
                pending.extend(char)
                if char in (b"\r", b"\n") or any(
                    pending.endswith(prompt.encode("ascii"))
                    for prompt in (self.READY_PROMPT, self.RETRY_PROMPT)
                ):
                    line = pending.decode("utf-8", errors="replace").strip()
                    if line:
                        self.lines.put(line)
                    pending.clear()
        finally:
            if pending.strip():
                self.lines.put(pending.decode("utf-8", errors="replace").strip())
            self.lines.put(None)

    def _diagnostic(self, message: str) -> str:
        output = " | ".join(self.recent_output) or "no output received"
        return f"{message}. Last spotread output: {output}"

    def _next_output(self, deadline: float, stage: str) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(self._diagnostic(f"spotread {stage} timed out"))
        try:
            line = self.lines.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError(self._diagnostic(f"spotread {stage} timed out")) from None
        if line is None:
            raise RuntimeError(self._diagnostic(f"spotread exited during {stage}"))
        self.recent_output.append(line)
        self.log.write("METER | " + line)
        if any(marker in line.lower() for marker in (
            "failed to initialise", "communications failure", "measuring refresh rate failed",
            "spot read failed", "spotread: error", "calibration failed",
        )):
            raise RuntimeError(self._diagnostic(f"spotread failed during {stage}"))
        return line

    def _start(self) -> None:
        self.log.write("METER starting persistent Argyll session: " + subprocess.list2cmdline(self.command))
        environment = os.environ.copy()
        # Required on Windows for piped stdin instead of console keystrokes.
        environment["ARGYLL_NOT_INTERACTIVE"] = "1"
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.reader_thread = threading.Thread(target=self._reader, args=(self.process.stdout,), daemon=True)
        self.reader_thread.start()
        deadline = time.monotonic() + self.startup_timeout
        while True:
            line = self._next_output(deadline, "startup")
            if self.READY_PROMPT in line:
                self.ready = True
                self.log.write("METER READY persistent session")
                return

    def read(self) -> XYZ:
        if not self.process or not self.process.stdin or not self.ready:
            raise RuntimeError("Meter is not running")
        self.ready = False
        try:
            # Argyll requires trigger and newline in ONE Windows pipe write.
            self.process.stdin.write(b" \n")
            self.process.stdin.flush()
            deadline = time.monotonic() + self.read_timeout
            result = None
            while True:
                line = self._next_output(deadline, "measurement")
                match = XYZ_RE.search(line)
                if match:
                    if result is not None:
                        raise RuntimeError(self._diagnostic("Multiple XYZ results for one trigger"))
                    values = tuple(float(value) for value in match.groups())
                    if not all(math.isfinite(value) for value in values):
                        raise RuntimeError(self._diagnostic("Non-finite XYZ result"))
                    result = XYZ(*values)
                if self.READY_PROMPT in line:
                    if result is None:
                        raise RuntimeError(self._diagnostic("spotread returned to its prompt without an XYZ result"))
                    # Early input can abort a measurement. Consume the next
                    # ready prompt before accepting another trigger.
                    self.ready = True
                    return result
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        process = self.process
        self.ready = False
        if process is None:
            return
        try:
            if process.poll() is None:
                try:
                    if process.stdin:
                        process.stdin.write(b"q\n")
                        process.stdin.flush()
                    # A quit key can first abort the armed measurement.
                    # Argyll then asks for a second quit confirmation.
                    deadline = time.monotonic() + 2.0
                    while process.poll() is None and time.monotonic() < deadline:
                        try:
                            line = self.lines.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                        except queue.Empty:
                            continue
                        if line is None:
                            break
                        self.log.write("METER | " + line)
                        if self.RETRY_PROMPT in line and process.stdin:
                            process.stdin.write(b"q\n")
                            process.stdin.flush()
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except (OSError, subprocess.TimeoutExpired):
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
        finally:
            if self.reader_thread:
                self.reader_thread.join(timeout=2)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()
            self.process = None


def measure(pattern: PatternHost, meter: Meter, log: Transcript, level: int,
            count: int, settle_seconds: float, stage: str) -> Measurement:
    pattern.show(level)
    time.sleep(settle_seconds)
    readings = []
    for number in range(1, count + 1):
        log.write(f"MEASURE stage={stage} level={level}% read={number}/{count}")
        readings.append(meter.read())
    centre = median_xyz(readings)
    x, y, _ = xyz_to_xyy(centre)
    u, v = xyz_to_uv(centre)
    uv_radii = []
    y_spreads = []
    for sample in readings:
        sample_u, sample_v = xyz_to_uv(sample)
        uv_radii.append(math.hypot(sample_u - u, sample_v - v))
        y_spreads.append(abs(sample.Y - centre.Y) / max(centre.Y, 1e-12))
    result = Measurement(
        level=level,
        xyz=centre,
        x=x,
        y=y,
        u=u,
        v=v,
        read_count=count,
        uv_noise=max(uv_radii, default=0.0),
        y_noise=max(y_spreads, default=0.0),
    )
    log.write(
        f"POINT stage={stage} level={level}% Y={centre.Y:.6f} "
        f"xy=({x:.5f},{y:.5f}) noise_uv={result.uv_noise:.6f} "
        f"noise_Y={result.y_noise:.2%}"
    )
    return result


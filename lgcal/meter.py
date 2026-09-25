"""Persistent ArgyllCMS spotread session.

This stands in for PGenerator's meter_session.sh: one spotread process is
kept open and each reading is one trigger, so there is no per-reading
start-up cost.
"""
from __future__ import annotations

import math
import os
import queue
import re
import subprocess
import threading
import time
from collections import deque

XYZ_RE = re.compile(r"Result is XYZ:\s*([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)")


class Meter:
    """read_timeout: a colorimeter can integrate for a long time near black;
    the worker itself waits up to 210 s for a reading at 5% and below."""
    READY_PROMPT = "any other key to take a reading:"
    RETRY_PROMPT = "any other key to retry:"

    def __init__(self, command: list[str], log, *, startup_timeout: float = 30.0,
                 read_timeout: float = 150.0):
        self.command = [str(part) for part in command]
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
            self.close()
            raise

    def _reader(self, stream, lines: queue.Queue) -> None:
        pending = bytearray()
        try:
            # Argyll prints its prompt WITHOUT a newline; readline() would
            # block while spotread waits for the trigger.
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
                        lines.put(line)
                    pending.clear()
        finally:
            if pending.strip():
                lines.put(pending.decode("utf-8", errors="replace").strip())
            lines.put(None)

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
        self.log("METER | " + line)
        if any(marker in line.lower() for marker in (
            "failed to initialise", "communications failure", "measuring refresh rate failed",
            "spot read failed", "spotread: error", "calibration failed",
        )):
            raise RuntimeError(self._diagnostic(f"spotread failed during {stage}"))
        return line

    def _start(self) -> None:
        self.log("METER starting spotread: " + subprocess.list2cmdline(self.command))
        environment = os.environ.copy()
        # Required on Windows for piped stdin instead of console keystrokes.
        environment["ARGYLL_NOT_INTERACTIVE"] = "1"
        self.lines = queue.Queue()
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.reader_thread = threading.Thread(
            target=self._reader, args=(self.process.stdout, self.lines), daemon=True)
        self.reader_thread.start()
        deadline = time.monotonic() + self.startup_timeout
        try:
            while True:
                if self.READY_PROMPT in self._next_output(deadline, "startup"):
                    self.ready = True
                    self.log("METER ready")
                    return
        except (TimeoutError, RuntimeError) as exc:
            raise RuntimeError(f"The meter did not start ({exc}). Check it is plugged in and not open in "
                               "another program (DisplayCAL, HCFR, Calman).") from None

    def restart(self) -> None:
        self.close()
        self.recent_output.clear()
        self._start()

    def read(self) -> tuple[float, float, float]:
        if not self.process or not self.process.stdin or not self.ready:
            raise RuntimeError("Meter is not running")
        self.ready = False
        try:
            # Argyll needs trigger and newline in ONE Windows pipe write.
            self.process.stdin.write(b" \n")
            self.process.stdin.flush()
            deadline = time.monotonic() + self.read_timeout
            result = None
            while True:
                line = self._next_output(deadline, "measurement")
                match = XYZ_RE.search(line)
                if match:
                    values = tuple(float(value) for value in match.groups())
                    if not all(math.isfinite(value) for value in values):
                        raise RuntimeError(self._diagnostic("Non-finite XYZ result"))
                    result = values
                if self.READY_PROMPT in line:
                    if result is None:
                        raise RuntimeError(self._diagnostic("spotread returned to its prompt without a result"))
                    # Early input can abort a measurement, so only accept the
                    # next trigger after the following ready prompt.
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
                    deadline = time.monotonic() + 2.0
                    while process.poll() is None and time.monotonic() < deadline:
                        try:
                            line = self.lines.get(timeout=0.1)
                        except queue.Empty:
                            continue
                        if line is None:
                            break
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


def read_with_recovery(meter, log, attempts: int = 3) -> tuple[float, float, float]:
    """One reading; a USB hiccup or prompt timeout restarts spotread."""
    for attempt in range(1, attempts + 1):
        try:
            return meter.read()
        except (TimeoutError, RuntimeError, OSError) as exc:
            restart = getattr(meter, "restart", None)
            if attempt == attempts or restart is None:
                raise
            log(f"METER RECOVERY attempt {attempt}: {exc}")
            restart()
    raise RuntimeError("unreachable")


def xyz_record(xyz: tuple[float, float, float]) -> dict:
    """The reading fields PGenerator's pgen_meter_result.py reports."""
    X, Y, Z = xyz
    total = X + Y + Z
    x, y = (X / total, Y / total) if total > 0 else (0.0, 0.0)
    cct = 0
    if y > 0:
        # McCamy's approximation, as PGenerator's result parser uses.
        n = (x - 0.3320) / (0.1858 - y)
        cct = int(round(449 * n ** 3 + 3525 * n ** 2 + 6823.3 * n + 5520.33))
        if not 1000 <= cct <= 25000:
            cct = 0
    return {"X": X, "Y": Y, "Z": Z, "luminance": Y, "x": x, "y": y, "cct": cct}

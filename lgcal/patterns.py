"""Full-screen patch window on the TV (stands in for PGenerator's renderer)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def replace_with_retry(source: Path, target: Path, timeout: float = 2.0) -> None:
    """os.replace, waiting out the brief sharing lock Windows takes while
    the pattern host reads the command file."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def to_8bit(code: int, input_max: int) -> int:
    """PGenerator scales a code by its input_max; Windows draws 8 bits."""
    input_max = input_max if input_max > 0 else 255
    return max(0, min(255, int(code * 255 / input_max + 0.5)))


def window_area(size: int) -> float:
    """PGenerator's patch size is a percentage of screen AREA. Sizes 101+
    are APL patterns on the Pi; they are drawn here as a plain 10% window."""
    if size >= 101:
        return 0.10
    return max(1, min(100, size)) / 100


class PatternWindow:
    def __init__(self, directory: Path, log, screen: str = ""):
        self.control = directory / "pattern_command.json"
        self.status = directory / "pattern_status.json"
        self.errors_path = directory / "pattern_host_errors.log"
        self.errors = self.errors_path.open("w", encoding="utf-8")
        self.log = log
        self.screen = screen
        self.sequence = 0
        self.process: subprocess.Popen | None = None
        self.device = ""
        self.dispwin: Path | None = None
        self.dispwin_index: int | None = None
        self.saved_lut: Path | None = None

    def _write(self, r=0, g=0, b=0, area=1.0, close=False) -> None:
        self.sequence += 1
        data = {"sequence": self.sequence, "r": r, "g": g, "b": b,
                "window_area": area, "close": close}
        temporary = self.control.with_suffix(".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        replace_with_retry(temporary, self.control)

    def start(self) -> None:
        self._write()
        command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                   str(Path(__file__).resolve().parent / "pattern_host.ps1"),
                   "-ControlFile", str(self.control), "-StatusFile", str(self.status)]
        if self.screen:
            command += ["-Screen", self.screen]
        self.process = subprocess.Popen(command, stdout=self.errors, stderr=subprocess.STDOUT,
                                        creationflags=NO_WINDOW)
        status = self._wait(self.sequence, 15.0)
        self.device = str(status["device"])
        self.log(f"PATTERN window on {self.device} {status['width']}x{status['height']}")

    def show(self, r: int, g: int, b: int, area: float) -> None:
        if not self.process or self.process.poll() is not None:
            raise RuntimeError("Pattern window is not running: " + self.error_text())
        self._write(r, g, b, area)
        self._wait(self.sequence, 5.0)

    def _wait(self, sequence: int, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process and self.process.poll() is not None:
                raise RuntimeError("Pattern window exited: " + self.error_text())
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
            self.errors.flush()
            return self.errors_path.read_text(encoding="utf-8")[-1000:]
        except Exception:
            return ""

    # A calibrated Windows profile loads a gamma ramp into the GPU, which
    # would change every code on its way to the TV. Clear it for the run and
    # put it back afterwards.
    def linearize_video_lut(self, dispwin: Path) -> None:
        listing = subprocess.run([str(dispwin), "-?"], capture_output=True, text=True,
                                 creationflags=NO_WINDOW, timeout=15)
        text = (listing.stdout or "") + "\n" + (listing.stderr or "")
        wanted = self.device.upper().removeprefix("\\\\.\\")
        index = None
        for match in re.finditer(r"^\s*(\d+)\s*=\s*'([^,']+)", text, re.MULTILINE):
            if match.group(2).strip().upper() == wanted:
                index = int(match.group(1))
        if index is None:
            raise RuntimeError(f"dispwin could not find the pattern display {self.device!r}")
        saved = self.control.parent / "original_video_lut.cal"
        run = subprocess.run([str(dispwin), "-d", str(index), "-s", str(saved)],
                             capture_output=True, text=True, creationflags=NO_WINDOW, timeout=15)
        if run.returncode != 0 or not saved.is_file():
            raise RuntimeError("Could not save the TV's video LUT: " + (run.stdout + run.stderr).strip())
        self.dispwin, self.dispwin_index, self.saved_lut = dispwin, index, saved
        run = subprocess.run([str(dispwin), "-d", str(index), "-c"],
                             capture_output=True, text=True, creationflags=NO_WINDOW, timeout=15)
        if run.returncode != 0:
            raise RuntimeError("Could not load a linear video LUT: " + (run.stdout + run.stderr).strip())
        self.log(f"PATTERN video LUT linearised (dispwin display {index})")

    def restore_video_lut(self) -> None:
        if not self.dispwin or self.dispwin_index is None or not self.saved_lut:
            return
        run = subprocess.run([str(self.dispwin), "-d", str(self.dispwin_index), str(self.saved_lut)],
                             capture_output=True, text=True, creationflags=NO_WINDOW, timeout=15)
        self.log("PATTERN original video LUT restored" if run.returncode == 0
                 else "PATTERN could not restore the video LUT: " + (run.stdout + run.stderr).strip())
        self.dispwin = self.dispwin_index = self.saved_lut = None

    def close(self) -> None:
        try:
            self.restore_video_lut()
        finally:
            if self.process and self.process.poll() is None:
                try:
                    self._write(close=True)
                    self.process.wait(timeout=3)
                except Exception:
                    self.process.terminate()
            self.errors.close()

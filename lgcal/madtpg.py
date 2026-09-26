"""madTPG (madVR's test pattern generator) as the HDR pattern source.

A Windows desktop window cannot send HDR10: PGenerator's renderer does it
on the Pi, and on a PC madTPG does it (DisplayCAL, HCFR and Calman drive it
the same way). madTPG is controlled through madHcNet64.dll; the calls and
their types follow DisplayCAL's madvr.py. ShowRGB takes 0..1 values; madVR
converts them to the output levels set in its own settings, and in HDR
mode sends them as the PQ signal with HDR10 metadata.

HDR mode itself is a button in the madTPG window (there is no call for
it), so the launcher asks for one click and then confirms HDR from the
picture mode the TV reports.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import time
from pathlib import Path

from .setup import TOOLS, download, extract

MADVR_URL = "http://madshi.net/madVR.zip"
CLSID = "{E1A8B82A-32CE-4B0D-BE0D-AA68C772E423}"      # madVR's COM class (madHcNet sits beside it)
CM_CONNECT_LOCAL, CM_START_LOCAL, CM_FAIL = 0, 2, 5
BOOL_CALLS = ("ConnectEx", "Disable3dlut", "EnterFullscreen", "LeaveFullscreen", "SetPatternConfig",
              "SetDisableOsdButton", "SetUseFullscreenButton", "SetStayOnTopButton", "SetOsdText",
              "ShowRGB", "Disconnect", "GetBlackAndWhiteLevel", "GetVersion")


def registered_madvr() -> Path | None:
    """The madVR folder registered on this PC, if any."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{CLSID}\InprocServer32") as key:
            value, _ = winreg.QueryValueEx(key, "")
    except (ImportError, OSError):
        return None
    folder = Path(value).parent
    return folder if (folder / "madTPG.exe").exists() else None


def find_madvr(settings: dict, say) -> Path:
    """settings 'madvr' folder, an installed madVR, or madVR downloaded into
    tools\\madVR (madshi's official zip; it runs from its folder)."""
    chosen = settings.get("madvr") or ""
    if chosen:
        folder = Path(chosen)
        if not (folder / "madTPG.exe").exists():
            raise SystemExit(f"settings.json madvr: {chosen} has no madTPG.exe.")
        return folder
    for folder in (registered_madvr(), TOOLS / "madVR"):
        if folder and (folder / "madTPG.exe").exists() and (folder / "madHcNet64.dll").exists():
            return folder
    say("madVR (for its HDR test pattern generator, madTPG) is not installed. Downloading it (one time) ...")
    archive = TOOLS / "madVR.zip"
    download(MADVR_URL, archive, say)
    extract(archive, TOOLS / "madVR", say)
    matches = sorted((TOOLS / "madVR").rglob("madTPG.exe"))
    if not matches:
        raise SystemExit("The madVR download does not contain madTPG.exe. Install madVR from madshi.net "
                         "and set \"madvr\" in settings.json to its folder.")
    return matches[0].parent


class MadHcNet:
    """The few madHcNet64.dll calls the run needs."""

    def __init__(self, dll_path: Path):
        self.dll = ctypes.WinDLL(str(dll_path))
        for name in BOOL_CALLS:
            getattr(self.dll, "madVR_" + name).restype = ctypes.c_bool
        uint = ctypes.c_uint
        self.dll.madVR_ConnectEx.argtypes = [ctypes.c_int, uint, ctypes.c_int, uint, ctypes.c_int, uint,
                                             ctypes.c_int, uint, ctypes.c_void_p]
        self.dll.madVR_ShowRGB.argtypes = [ctypes.c_double] * 3
        self.dll.madVR_SetPatternConfig.argtypes = [ctypes.c_int] * 4
        self.dll.madVR_SetOsdText.argtypes = [ctypes.c_wchar_p]
        for name in ("SetDisableOsdButton", "SetUseFullscreenButton", "SetStayOnTopButton"):
            getattr(self.dll, "madVR_" + name).argtypes = [ctypes.c_bool]
        self.dll.madVR_GetBlackAndWhiteLevel.argtypes = [ctypes.POINTER(ctypes.c_int)] * 2

    def connect(self, timeout_ms: int) -> bool:
        return self.dll.madVR_ConnectEx(CM_CONNECT_LOCAL, timeout_ms, CM_START_LOCAL, timeout_ms,
                                        CM_FAIL, 0, CM_FAIL, 0, None)

    def levels(self) -> tuple[int, int] | None:
        black, white = ctypes.c_int(), ctypes.c_int()
        return (black.value, white.value) if self.dll.madVR_GetBlackAndWhiteLevel(
            ctypes.byref(black), ctypes.byref(white)) else None

    def __getattr__(self, name: str):
        return getattr(self.dll, "madVR_" + name)


class MadTPGPatterns:
    """The pattern interface the run uses (show / show_code / close),
    drawn by madTPG. show() takes 8-bit codes; show_code() keeps the
    worker's 10-bit precision."""

    def __init__(self, folder: Path, log, api=None, launch=True):
        self.log = log
        self.process = None
        if launch and api is None:
            self.process = subprocess.Popen([str(folder / "madTPG.exe")], cwd=str(folder))
        self.api = api or MadHcNet(folder / "madHcNet64.dll")
        if not self.api.connect(15000):
            self.close()
            raise SystemExit("Could not connect to madTPG. Close any other madTPG window and run again.")
        self.area = None
        self.api.Disable3dlut()
        self.api.SetDisableOsdButton(True)
        self.api.SetStayOnTopButton(True)
        self.api.SetUseFullscreenButton(True)
        levels = self.api.levels() if hasattr(self.api, "levels") else None
        if levels:
            log(f"madTPG output levels {levels[0]}-{levels[1]}")

    def _area(self, area: float) -> None:
        percent = max(1, min(100, int(round(area * 100))))
        if percent != self.area:
            self.api.SetPatternConfig(percent, 0, 0, 0)
            self.area = percent

    def show(self, r: int, g: int, b: int, area: float) -> None:
        self.show_code(r, g, b, 255, area)

    def show_code(self, r: float, g: float, b: float, input_max: int, area: float) -> None:
        self._area(area)
        top = float(input_max if input_max > 0 else 255)
        if not self.api.ShowRGB(*(min(1.0, max(0.0, v / top)) for v in (r, g, b))):
            raise RuntimeError("madTPG stopped showing patterns (its window was closed?)")

    def message(self, text: str) -> None:
        self.api.SetOsdText(text)

    def close(self) -> None:
        try:
            self.api.Disconnect()
        except Exception as exc:  # the window may already be gone
            self.log(f"madTPG disconnect: {exc!r}")
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()


def wait_for_hdr(lg, say, modes, timeout: float = 600, clock=time.monotonic, sleep=time.sleep) -> str:
    """Wait until the TV reports an HDR picture mode (madTPG's HDR button
    switches the TV into HDR). Returns that mode."""
    say("madTPG is open. Once only:")
    say("  1. Drag the madTPG window onto the TV and double-click it for fullscreen.")
    say("  2. Click its 'HDR' button (in the button's menu: BT.2020, 1000 nits).")
    say("  3. On the TV, pick Cinema, Cinema Home or Filmmaker (HDR) and set Dynamic Tone Mapping to Off.")
    say("The run continues by itself when the TV switches to an HDR mode LG can calibrate.")
    started = clock()
    shown = None
    while clock() - started < timeout:
        mode = lg.current_picture_mode()
        if mode in modes:
            return mode
        if mode.lower().startswith(("hdr", "dolby")) and mode != shown:
            say(f"  The TV is in HDR but in {mode}, which LG does not accept calibration for. Pick Cinema, "
                "Cinema Home, Filmmaker or Game Optimizer on the TV.")
            shown = mode
        sleep(2)
    raise SystemExit("The TV did not switch to an HDR picture mode within 10 minutes. Check madTPG's HDR "
                     "button is on and Windows HDR is off for the TV, then run again.")

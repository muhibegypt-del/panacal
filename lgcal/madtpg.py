"""madTPG (madVR's test pattern generator) as the HDR pattern source.

A Windows desktop window cannot send HDR10: PGenerator's renderer does it
on the Pi, and on a PC madTPG does it (DisplayCAL, HCFR and Calman drive it
the same way). madTPG is controlled through madHcNet64.dll; the calls and
their types follow DisplayCAL's madvr.py. ShowRGB takes 0..1 values; madVR
converts them to the output levels set in its own settings, and in HDR
mode sends them as the PQ signal with HDR10 metadata.

The calls follow madshi's own interface description (madTPG.h in the
madVR zip): the window is placed on the TV and made fullscreen, and the
HDR button is pressed with BT.2020 / D65 / 1000-nit metadata, so the run
only needs the TV's HDR picture mode picked. Anything madTPG refuses falls
back to asking for the click.
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
BOOL_CALLS = ("Connect", "ConnectEx", "Disable3dlut", "EnterFullscreen", "LeaveFullscreen", "IsFullscreen",
              "SetPatternConfig", "SetDisableOsdButton", "SetUseFullscreenButton", "SetStayOnTopButton",
              "SetOsdText", "ShowRGB", "Disconnect", "GetBlackAndWhiteLevel", "GetVersion", "SetHdrButton",
              "IsHdrButtonPressed", "SetHdrMetadata", "SetWindowSize", "SetDeviceGammaRamp")
# SMPTE 2086 metadata madTPG sends in HDR mode: BT.2020 primaries, D65, a
# 1000-nit mastering display (LG's reference for its HDR calibration).
HDR_METADATA = (0.708, 0.292, 0.170, 0.797, 0.131, 0.046, 0.3127, 0.3290, 0.0001, 1000.0, 1000.0, 400.0)


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
    candidates = [registered_madvr()] + sorted(p.parent for p in (TOOLS / "madVR").rglob("madTPG.exe"))
    for folder in candidates:        # the official zip unpacks into a madVR\ subfolder
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
        for name in ("Connect", "ConnectEx"):
            getattr(self.dll, "madVR_" + name).argtypes = [ctypes.c_int, uint, ctypes.c_int, uint, ctypes.c_int,
                                                           uint, ctypes.c_int, uint, ctypes.c_void_p]
        self.dll.madVR_ShowRGB.argtypes = [ctypes.c_double] * 3
        self.dll.madVR_SetPatternConfig.argtypes = [ctypes.c_int] * 4
        self.dll.madVR_SetOsdText.argtypes = [ctypes.c_wchar_p]
        for name in ("SetDisableOsdButton", "SetUseFullscreenButton", "SetStayOnTopButton", "SetHdrButton"):
            getattr(self.dll, "madVR_" + name).argtypes = [ctypes.c_bool]
        self.dll.madVR_SetHdrMetadata.argtypes = [ctypes.c_double] * 12
        self.dll.madVR_SetWindowSize.argtypes = [ctypes.c_void_p]
        self.dll.madVR_SetDeviceGammaRamp.argtypes = [ctypes.c_void_p]
        self.dll.madVR_GetBlackAndWhiteLevel.argtypes = [ctypes.POINTER(ctypes.c_int)] * 2

    def connect(self, timeout_ms: int) -> bool:
        # madVR_Connect is the documented call; ConnectEx is what DisplayCAL uses.
        for name in ("Connect", "ConnectEx"):
            if getattr(self.dll, "madVR_" + name)(CM_CONNECT_LOCAL, timeout_ms, CM_START_LOCAL, timeout_ms,
                                                  CM_FAIL, 0, CM_FAIL, 0, None):
                return True
        return False

    def place(self, left: int, top: int, right: int, bottom: int) -> bool:
        rect = (ctypes.c_long * 4)(left, top, right, bottom)
        return self.dll.madVR_SetWindowSize(ctypes.byref(rect))

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

    def __init__(self, folder: Path, log, api=None, launch=True, sleep=time.sleep):
        self.log = log
        self.sleep = sleep
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

    def to_screen(self, display: dict) -> bool:
        """Window onto the TV's desktop area, then fullscreen there.
        True once madTPG reports fullscreen."""
        try:
            x, y = int(display["x"]), int(display["y"])
            width, height = int(display["width"]), int(display["height"])
        except (KeyError, TypeError, ValueError):
            return False
        try:
            self.api.LeaveFullscreen()
            placed = self.api.place(x + width // 4, y + height // 4, x + 3 * width // 4, y + 3 * height // 4)
            fullscreen = placed and self.api.EnterFullscreen()
            self.sleep(1)
            fullscreen = fullscreen and self.api.IsFullscreen()
        except Exception as exc:
            self.log(f"madTPG window placement failed: {exc!r}")
            return False
        self.log(f"madTPG window on {display.get('device')} at {x},{y} {width}x{height}: "
                 f"placed={bool(placed)} fullscreen={bool(fullscreen)}")
        return bool(fullscreen)

    def hdr_on(self) -> bool:
        """HDR metadata set and the HDR button pressed; True once madTPG
        reports the button down."""
        try:
            self.api.SetHdrMetadata(*HDR_METADATA)
            self.api.SetHdrButton(True)
            self.sleep(1)
            pressed = self.api.IsHdrButtonPressed()
        except Exception as exc:
            self.log(f"madTPG HDR button failed: {exc!r}")
            return False
        self.log(f"madTPG HDR button pressed={bool(pressed)}")
        return bool(pressed)

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


def wait_for_hdr(lg, say, modes, timeout: float = 600, clock=time.monotonic, sleep=time.sleep,
                 on_screen: bool = False, hdr: bool = False) -> str:
    """Wait until the TV reports an HDR picture mode (madTPG's HDR button
    switches the TV into HDR). Returns that mode. on_screen / hdr: madTPG
    already went fullscreen on the TV / pressed its HDR button itself."""
    steps = []
    if not on_screen:
        steps.append("Drag the madTPG window onto the TV and double-click it for fullscreen.")
    if not hdr:
        steps.append("Click madTPG's 'HDR' button (in the button's menu: BT.2020, 1000 nits).")
    steps.append("On the TV, pick Cinema, Cinema Home or Filmmaker (HDR) and set Dynamic Tone Mapping to Off.")
    mode = lg.current_picture_mode()
    if mode in modes:
        return mode
    say(("madTPG is fullscreen on the TV" if on_screen else "madTPG is open")
        + (" and sending HDR10 (BT.2020, 1000 nits)." if hdr else ".") + " Once only:")
    for number, step in enumerate(steps, 1):
        say(f"  {number}. {step}")
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

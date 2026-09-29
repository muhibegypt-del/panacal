"""CRT Low Slider: red, green and blue low-end trims for a CRT whose
cutoffs have drifted, applied through the graphics card's gamma ramp.

A drifted cutoff means one gun starts glowing later (or earlier) than the
others, so dark greys take a tint that fades towards white. The monitor's
own cutoffs are set by its factory routine only; this is the software
stand-in: each slider shifts that channel's signal near black, the way an
analogue bias control does, while black (0) and white (255) stay exactly
where they are.

Guard rails:
  * black and white never move, and every curve rises monotonically;
  * trims are limited to +-16 of 255 levels;
  * the curve's steepness is kept between 0.6x and 1.7x of normal: if a
    trim would bunch levels up near black, the ramp-in is widened
    instead, so no step gets bigger than about two levels;
  * the banding meter shows how many 8-bit levels the curve merges and
    the largest step it makes (with a 10-bit output or GPU dithering the
    ramp's 16-bit values are used in full and nothing merges);
  * nothing is kept unless Save is pressed: closing the window puts the
    ramp back as it was; if other software resets the ramp while the
    window is open, it is put back.

    CRT Low Slider.bat            the window
    crt_low_slider.py --apply     apply the saved trims (for login)
    crt_low_slider.py --reset     put a linear ramp on every display
    crt_low_slider.py --diagnose  test tint on each display, report what happened
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SETTINGS = HERE / "crt_low.json"
STARTUP_NAME = "CRT Low Slider.bat"

MAX_TRIM = 16.0            # 8-bit levels
SLOPE_MIN, SLOPE_MAX = 0.6, 1.7
KNEE_MIN, KNEE_MAX = 0.02, 0.30
REACH_MIN, REACH_MAX = 1.0, 4.0
DEFAULTS = {"red": 0.0, "green": 0.0, "blue": 0.0, "reach": 1.5, "knee": 0.03}
CHANNELS = ("red", "green", "blue")


# --- the curve (pure; tested offline) -----------------------------------------

def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def shape(x: float, reach: float, knee: float) -> float:
    """How much of the trim applies at signal x (0..1): nothing at black,
    ramping in smoothly over the knee, then fading towards white.
    reach 1 fades linearly like an analogue bias control; higher reach
    keeps it to the dark end."""
    if x <= 0.0 or x >= 1.0:
        return 0.0
    t = min(1.0, x / knee)
    return t * t * (3 - 2 * t) * (1 - x) ** reach


def curve(trim: float, reach: float, knee: float) -> list[float]:
    """256 output levels (0..255, fractional) for one channel."""
    out = [_clamp(i + trim * shape(i / 255, reach, knee), 0.0, 255.0) for i in range(256)]
    for i in range(1, 256):                       # never let a level fall below the one before
        out[i] = max(out[i], out[i - 1])
    out[0], out[255] = 0.0, 255.0
    return out


def slopes(levels: list[float]) -> list[float]:
    return [levels[i + 1] - levels[i] for i in range(len(levels) - 1)]


def safe_knee(trim: float, reach: float, knee: float) -> float:
    """The smallest ramp-in width (at least `knee`) that keeps the curve's
    steepness within SLOPE_MIN..SLOPE_MAX."""
    knee = _clamp(knee, KNEE_MIN, KNEE_MAX)
    while knee < KNEE_MAX:
        s = slopes(curve(trim, reach, knee))
        if SLOPE_MIN <= min(s) and max(s) <= SLOPE_MAX:
            return knee
        knee = min(KNEE_MAX, knee + 0.005)
    return KNEE_MAX


def fade_level(reach: float) -> int:
    """Signal % above which less than a tenth of the trim is left."""
    return round(100 * (1 - 0.1 ** (1 / reach)))


def banding(levels: list[float]) -> dict:
    """What the curve does at 8 bits: levels merged into a neighbour, and
    the largest jump between neighbouring levels."""
    rounded = [round(v) for v in levels]
    steps = [rounded[i + 1] - rounded[i] for i in range(255)]
    return {"merged": sum(1 for s in steps if s == 0), "largest_step": max(steps),
            "min_slope": round(min(slopes(levels)), 3), "max_slope": round(max(slopes(levels)), 3)}


def clean(settings: dict) -> dict:
    """Settings limited to what the guard rails allow."""
    out = dict(DEFAULTS)
    for key in CHANNELS:
        out[key] = _clamp(float(settings.get(key, 0.0)), -MAX_TRIM, MAX_TRIM)
    out["reach"] = _clamp(float(settings.get("reach", DEFAULTS["reach"])), REACH_MIN, REACH_MAX)
    out["knee"] = _clamp(float(settings.get("knee", DEFAULTS["knee"])), KNEE_MIN, KNEE_MAX)
    return out


def build(settings: dict) -> tuple[list[list[int]], dict]:
    """The 3 x 256 16-bit gamma ramp and a per-channel report."""
    settings = clean(settings)
    ramp, report = [], {}
    for key in CHANNELS:
        knee = safe_knee(settings[key], settings["reach"], settings["knee"])
        levels = curve(settings[key], settings["reach"], knee)
        ramp.append([int(round(v * 257)) for v in levels])
        report[key] = {**banding(levels), "knee": knee, "widened": knee > settings["knee"] + 1e-9}
    return ramp, report


def identity() -> list[list[int]]:
    return [[i * 257 for i in range(256)] for _ in range(3)]


def same(a: list[list[int]] | None, b: list[list[int]] | None, tolerance: int = 300) -> bool:
    """Two ramps equal to within about one 8-bit level (drivers may hand
    back a ramp rounded to their own precision)."""
    if a is None or b is None:
        return a is b
    return all(abs(x - y) <= tolerance for ca, cb in zip(a, b) for x, y in zip(ca, cb))


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    SETTINGS.write_text(json.dumps(data, indent=1), encoding="utf-8")


# --- Windows ------------------------------------------------------------------

class Displays:
    """Monitors and their gamma ramps through user32/gdi32."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ctypes, self.w = ctypes, w
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass
        self.user32, self.gdi32 = ctypes.windll.user32, ctypes.windll.gdi32

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT),
                        ("dwFlags", w.DWORD), ("szDevice", w.WCHAR * 32)]

        class DisplayDevice(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("DeviceName", w.WCHAR * 32), ("DeviceString", w.WCHAR * 128),
                        ("StateFlags", w.DWORD), ("DeviceID", w.WCHAR * 128), ("DeviceKey", w.WCHAR * 128)]
        self.MonitorInfo, self.DisplayDevice = MonitorInfo, DisplayDevice
        self.MonitorProc = ctypes.WINFUNCTYPE(w.BOOL, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)
        self.Ramp = (ctypes.c_ushort * 256) * 3
        self.user32.EnumDisplayMonitors.argtypes = [w.HDC, ctypes.c_void_p, self.MonitorProc, w.LPARAM]
        self.user32.GetMonitorInfoW.argtypes = [w.HMONITOR, ctypes.POINTER(MonitorInfo)]
        self.user32.EnumDisplayDevicesW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(DisplayDevice), w.DWORD]
        self.gdi32.CreateDCW.argtypes = [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p]
        self.gdi32.CreateDCW.restype = w.HDC
        self.gdi32.DeleteDC.argtypes = [w.HDC]
        self.gdi32.SetDeviceGammaRamp.argtypes = [w.HDC, ctypes.c_void_p]
        self.gdi32.GetDeviceGammaRamp.argtypes = [w.HDC, ctypes.c_void_p]

    def monitors(self) -> list[dict]:
        found = []

        def each(monitor, _dc, _rect, _data):
            info = self.MonitorInfo()
            info.cbSize = self.ctypes.sizeof(info)
            if self.user32.GetMonitorInfoW(monitor, self.ctypes.byref(info)):
                r = info.rcMonitor
                found.append({"device": info.szDevice, "name": self._name(info.szDevice),
                              "primary": bool(info.dwFlags & 1),
                              "rect": (r.left, r.top, r.right - r.left, r.bottom - r.top)})
            return True
        self.user32.EnumDisplayMonitors(None, None, self.MonitorProc(each), 0)
        return found

    def _name(self, device: str) -> str:
        dd = self.DisplayDevice()
        dd.cb = self.ctypes.sizeof(dd)
        if self.user32.EnumDisplayDevicesW(device, 0, self.ctypes.byref(dd), 0):
            return dd.DeviceString
        return ""

    def _dc(self, device: str):
        # The display's own name as the driver argument gives a DC for that
        # display; ("DISPLAY", name) can give the main display's DC instead.
        dc = self.gdi32.CreateDCW(device, None, None, None)
        if not dc:
            raise OSError(f"cannot open {device}")
        return dc

    def get(self, device: str) -> list[list[int]] | None:
        dc, ramp = self._dc(device), self.Ramp()
        try:
            ok = self.gdi32.GetDeviceGammaRamp(dc, self.ctypes.byref(ramp))
        finally:
            self.gdi32.DeleteDC(dc)
        return [list(ramp[c]) for c in range(3)] if ok else None

    def set(self, device: str, values: list[list[int]]) -> bool:
        ramp = self.Ramp()
        for c in range(3):
            for i in range(256):
                ramp[c][i] = values[c][i]
        dc = self._dc(device)
        try:
            return bool(self.gdi32.SetDeviceGammaRamp(dc, self.ctypes.byref(ramp)))
        finally:
            self.gdi32.DeleteDC(dc)


def allow_wider_ramps() -> None:
    """Windows refuses gamma ramps far from linear unless this value is set
    (admin; Windows asks first)."""
    import ctypes
    args = (r'add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM" '
            r"/v GdiIcmGammaRange /t REG_DWORD /d 256 /f")
    ctypes.windll.shell32.ShellExecuteW(None, "runas", "reg.exe", args, None, 0)


def startup_file() -> Path:
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            / "Startup" / STARTUP_NAME)


def set_login(enabled: bool) -> None:
    target = startup_file()
    if not enabled:
        target.unlink(missing_ok=True)
        return
    script = HERE / "crt_low_slider.py"
    target.write_text("@echo off\r\n"
                      f'cd /d "{HERE}"\r\n'
                      "where pyw >nul 2>&1\r\n"
                      f'if %errorlevel%==0 (start "" pyw -3 "{script}" --apply) '
                      f'else (start "" pythonw "{script}" --apply)\r\n', encoding="utf-8")


def pick_default(monitors: list[dict], saved_device: str) -> dict:
    for m in monitors:
        if m["device"] == saved_device:
            return m
    for m in monitors:
        if "iiyama" in m["name"].lower() or "hm204" in m["name"].lower():
            return m
    secondary = [m for m in monitors if not m["primary"]]
    return secondary[0] if len(secondary) == 1 else monitors[0]


# --- command line -------------------------------------------------------------

def apply_saved(displays: Displays, hold_seconds: float = 60.0) -> int:
    """Apply the saved trims, and put them back for a minute in case a
    colour-profile loader resets the ramp during login."""
    data = load_settings()
    device = data.get("device")
    if not device:
        return 1
    ramp, _ = build(data)
    ended = time.monotonic() + hold_seconds
    while True:
        if not same(displays.get(device), ramp):
            displays.set(device, ramp)
        if time.monotonic() >= ended:
            return 0
        time.sleep(2)


def reset_all(displays: Displays) -> int:
    for m in displays.monitors():
        displays.set(m["device"], identity())
    return 0


def diagnose(displays: Displays) -> int:
    """Try a visible test tint on every display and write what Windows and
    the driver did with it to crt_low_diagnose.txt."""
    import ctypes
    import platform
    lines = [f"Windows {platform.version()} ({platform.release()}), Python {platform.python_version()}"]
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM") as key:
            lines.append(f"GdiIcmGammaRange = {winreg.QueryValueEx(key, 'GdiIcmGammaRange')[0]}")
    except OSError:
        lines.append("GdiIcmGammaRange not set (Windows default limits)")
    tint = identity()
    tint[1] = [round(v * 0.6) for v in tint[1]]
    for m in displays.monitors():
        lines.append("")
        lines.append(f'{m["device"]}  "{m["name"]}"  primary={m["primary"]}  rect={m["rect"]}')
        try:
            before = displays.get(m["device"])
            lines.append("  read ramp: " + ("failed" if before is None else
                                            "linear" if same(before, identity(), 64) else
                                            f"NOT linear (green at 50%: {before[1][128]} of {128 * 257})"))
            accepted = displays.set(m["device"], tint)
            after = displays.get(m["device"])
            lines.append(f"  test tint accepted by Windows: {accepted}")
            lines.append("  reads back as the test tint: " + str(same(after, tint)))
            time.sleep(2)
            later = displays.get(m["device"])
            lines.append("  still the test tint 2 s later: " + str(same(later, tint)))
            displays.set(m["device"], before or identity())
        except OSError as exc:
            lines.append(f"  error: {exc}")
    report = HERE / "crt_low_diagnose.txt"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ctypes.windll.user32.MessageBoxW(
        None, "Each display was tinted magenta for 2 seconds, one after another.\n\n"
              + "\n".join(lines) + f"\n\nSaved to {report}", "CRT Low Slider diagnosis", 0x40)
    return 0


# --- the window ---------------------------------------------------------------

def run_window(displays: Displays) -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    saved = load_settings()
    monitors = displays.monitors()
    if not monitors:
        messagebox.showerror("CRT Low Slider", "Windows reports no displays.")
        return 1
    state = {"monitor": pick_default(monitors, saved.get("device", "")), "original": {}, "ramp": None,
             "refused": False, "pending": None, "saved": dict(saved)}

    root = tk.Tk()
    root.title("CRT Low Slider")
    root.resizable(False, False)
    pad = {"padx": 10, "pady": 4}

    def remember_original(device: str) -> None:
        if device not in state["original"]:
            state["original"][device] = displays.get(device) or identity()

    frame = ttk.Frame(root, padding=10)
    frame.grid()
    ttk.Label(frame, text="Display").grid(row=0, column=0, sticky="w", **pad)
    names = [f'{m["device"].replace(chr(92) * 2 + "." + chr(92), "")}  {m["name"]}'
             f'{"  (main)" if m["primary"] else ""}' for m in monitors]
    choice = tk.StringVar(value=names[monitors.index(state["monitor"])])
    box = ttk.Combobox(frame, values=names, textvariable=choice, state="readonly", width=46)
    box.grid(row=0, column=1, columnspan=2, sticky="w", **pad)

    values = {k: tk.DoubleVar(value=float(saved.get(k, DEFAULTS[k])))
              for k in (*CHANNELS, "reach", "knee")}
    readouts = {}
    rows = [("red", "Red low", -MAX_TRIM, MAX_TRIM, 0.25),
            ("green", "Green low", -MAX_TRIM, MAX_TRIM, 0.25),
            ("blue", "Blue low", -MAX_TRIM, MAX_TRIM, 0.25),
            ("reach", "Reach", REACH_MIN, REACH_MAX, 0.1),
            ("knee", "Ramp-in", KNEE_MIN, KNEE_MAX, 0.005)]
    for r, (key, label, low, high, step) in enumerate(rows, start=1):
        ttk.Label(frame, text=label).grid(row=r, column=0, sticky="w", **pad)
        scale = tk.Scale(frame, from_=low, to=high, resolution=step, orient="horizontal", length=360,
                         variable=values[key], showvalue=False, command=lambda _v: schedule())
        scale.grid(row=r, column=1, sticky="w", **pad)
        readouts[key] = ttk.Label(frame, width=30)
        readouts[key].grid(row=r, column=2, sticky="w", **pad)

    meter = ttk.Label(frame, justify="left", font=("Consolas", 9))
    meter.grid(row=6, column=0, columnspan=3, sticky="w", **pad)
    status = ttk.Label(frame, justify="left", wraplength=640)
    status.grid(row=7, column=0, columnspan=3, sticky="w", **pad)

    buttons = ttk.Frame(frame)
    buttons.grid(row=8, column=0, columnspan=3, sticky="w", **pad)
    login = tk.BooleanVar(value=startup_file().exists())

    def current() -> dict:
        return {k: float(v.get()) for k, v in values.items()}

    def describe() -> None:
        s = current()
        for key in CHANNELS:
            trim = s[key]
            readouts[key].config(text=f"{trim:+.2f} levels  " + ("(more)" if trim > 0 else
                                                                  "(less)" if trim < 0 else ""))
        readouts["reach"].config(text=f"fades out by about {fade_level(s['reach'])}%")
        readouts["knee"].config(text=f"over the first {s['knee'] * 100:.1f}% above black")

    def apply() -> None:
        state["pending"] = None
        device = state["monitor"]["device"]
        remember_original(device)
        ramp, report = build(current())
        ok = displays.set(device, ramp)
        state["ramp"] = ramp if ok else None
        lines = []
        for key in CHANNELS:
            r = report[key]
            flag = "ok" if r["largest_step"] <= 2 and r["merged"] <= 12 else "check gradients"
            lines.append(f"{key:<6} 8-bit: {r['merged']:>2} levels merged, largest step {r['largest_step']}"
                         f"  steepness {r['min_slope']:.2f}-{r['max_slope']:.2f}  {flag}"
                         + (f"  (ramp-in widened to {r['knee'] * 100:.1f}%)" if r["widened"] else ""))
        meter.config(text="\n".join(lines))
        target = f'{state["monitor"]["name"] or device} ({device})'
        if ok and not same(displays.get(device), ramp):
            status.config(text=f"Windows accepted the curve for {target}, but the display's ramp reads back "
                               "unchanged, so the graphics driver is ignoring it. Check: NVIDIA/AMD colour "
                               "settings on 'other applications control colour'; Windows HDR and Auto colour "
                               "management off for this display; not a DisplayLink/USB display adapter.",
                          foreground="red")
        elif ok:
            state["refused"] = False
            status.config(text=f"Applied to {target}. Measure, adjust, repeat. Save keeps it; closing "
                               "without saving puts the display back. 'Identify' flashes the display "
                               "being adjusted.", foreground="")
            allow.pack_forget()
        else:
            state["refused"] = True
            status.config(text="Windows refused this curve (it limits how far a gamma ramp may move). "
                               "Press 'Allow wider curves' once (admin), then move a slider again.",
                          foreground="red")
            allow.pack(side="left", padx=4)

    def schedule() -> None:
        describe()
        if state["pending"]:
            root.after_cancel(state["pending"])
        state["pending"] = root.after(120, apply)

    def on_display(_event=None) -> None:
        old = state["monitor"]["device"]
        if old in state["original"] and old != state["saved"].get("device"):
            displays.set(old, state["original"][old])
        state["monitor"] = monitors[names.index(choice.get())]
        apply()

    def reset() -> None:
        for key in CHANNELS:
            values[key].set(0.0)
        values["reach"].set(DEFAULTS["reach"])
        values["knee"].set(DEFAULTS["knee"])
        schedule()

    def save() -> None:
        data = {**clean(current()), "device": state["monitor"]["device"], "name": state["monitor"]["name"]}
        save_settings(data)
        state["saved"] = data
        set_login(login.get())
        status.config(text="Saved. It stays applied after closing"
                           + (" and is applied at every login." if login.get() else
                              ". Tick 'Apply at login' to have it put back after a restart."),
                      foreground="green")

    def pattern() -> None:
        x, y, w, h = state["monitor"]["rect"]
        top = tk.Toplevel(root)
        top.overrideredirect(True)
        top.geometry(f"{w}x{h}{x:+d}{y:+d}")
        top.configure(background="black")
        canvas = tk.Canvas(top, width=w, height=h, highlightthickness=0, background="black")
        canvas.pack()
        bars = 21                                   # 0% to 20% in 1% steps
        for i in range(bars):
            code = round(i * 2.55)
            x0, x1 = round(i * w / bars), round((i + 1) * w / bars)
            canvas.create_rectangle(x0, 0, x1, h // 2, fill=f"#{code:02x}{code:02x}{code:02x}", width=0)
            canvas.create_text((x0 + x1) // 2, h // 2 - 20, text=f"{i}%", fill="#505050")
        top_code = 102                              # gradient 0% to 40%: steps show as banding
        for col in range(w):
            code = round(col * top_code / (w - 1))
            canvas.create_line(col, h // 2, col, h, fill=f"#{code:02x}{code:02x}{code:02x}")
        canvas.create_text(w // 2, h - 30, text="Top: 0-20% in 1% steps (look for tint).  Bottom: smooth "
                           "0-40% ramp (look for bands).  Esc closes.", fill="#606060")
        top.bind("<Escape>", lambda _e: top.destroy())
        top.bind("<Button-1>", lambda _e: top.destroy())
        top.focus_force()

    def identify() -> None:
        """Tint the chosen display magenta for a moment, to show which one the
        sliders act on and that the driver applies the ramp."""
        device = state["monitor"]["device"]
        remember_original(device)
        flash = identity()
        flash[1] = [round(v * 0.6) for v in flash[1]]
        if not displays.set(device, flash):
            status.config(text="Windows refused even the test tint for this display.", foreground="red")
            return
        root.after(1500, apply)

    def watchdog() -> None:
        device = state["monitor"]["device"]
        if state["ramp"] is not None and not state["pending"]:
            now = displays.get(device)
            if now is not None and not same(now, state["ramp"]):
                displays.set(device, state["ramp"])
                status.config(text="Something reset the display's ramp (a profile loader or the graphics "
                                   "driver); put it back.", foreground="orange")
        root.after(2000, watchdog)

    def close() -> None:
        kept = state["saved"].get("device")
        unsaved = clean(current()) != clean(state["saved"]) if kept else any(
            abs(float(values[k].get())) > 1e-9 for k in CHANNELS)
        if unsaved and not messagebox.askyesno(
                "CRT Low Slider", "Close without saving? The display goes back to how it was "
                                  "(or to the last saved trims)."):
            return
        for device, original in state["original"].items():
            if device == kept:
                ramp, _ = build(state["saved"])
                displays.set(device, ramp)
            else:
                displays.set(device, original)
        root.destroy()

    ttk.Button(buttons, text="Identify", command=identify).pack(side="left", padx=4)
    ttk.Button(buttons, text="Test pattern", command=pattern).pack(side="left", padx=4)
    ttk.Button(buttons, text="Reset to zero", command=reset).pack(side="left", padx=4)
    ttk.Button(buttons, text="Save", command=save).pack(side="left", padx=4)
    ttk.Checkbutton(buttons, text="Apply at login", variable=login).pack(side="left", padx=12)
    allow = ttk.Button(buttons, text="Allow wider curves", command=allow_wider_ramps)

    box.bind("<<ComboboxSelected>>", on_display)
    root.bind("<Control-r>", lambda _e: reset())
    root.protocol("WM_DELETE_WINDOW", close)
    describe()
    apply()
    original = state["original"].get(state["monitor"]["device"])
    if original and not same(original, identity()) and not saved:
        status.config(text="Note: this display had a non-linear ramp loaded (a colour profile or driver "
                           "setting). The sliders replace it while this window is open.", foreground="orange")
    root.after(2000, watchdog)
    root.mainloop()
    return 0


def main(argv: list[str]) -> int:
    if os.name != "nt":
        print("CRT Low Slider runs on Windows.")
        return 1
    try:
        displays = Displays()
        if "--apply" in argv:
            return apply_saved(displays)
        if "--reset" in argv:
            return reset_all(displays)
        if "--diagnose" in argv:
            return diagnose(displays)
        return run_window(displays)
    except Exception:                  # runs windowless (pythonw): say what went wrong
        import ctypes
        import traceback
        log = HERE / "crt_low_error.txt"
        log.write_text(traceback.format_exc(), encoding="utf-8")
        ctypes.windll.user32.MessageBoxW(None, f"CRT Low Slider stopped. Details are in {log}",
                                         "CRT Low Slider", 0x10)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

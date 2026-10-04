"""LG Game Setup: P40L0's optimized LG OLED gaming settings (TechOptimized
sheet v19) for a PC on one HDMI input, sent with bscpylgtv
(github.com/chros73/bscpylgtv) exactly as its README does it. Setting names
and values are a 2022 LG's (bscpylgtv's C2 settings dump).
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys

GPU_VENDORS = (("nvidia", "NVIDIA"), ("amd", "AMD"))
BLACK_LEVEL_KEYS = ("ntsc", "ntsc443", "pal", "pal60", "palm", "paln", "secam", "unknown")


def gpu_vendor(run=subprocess.run) -> str:
    """'nvidia' or 'amd' when the PC's graphics cards are all one make, else ''."""
    from .display import NO_WINDOW, powershell
    try:
        names = run([powershell(), "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
                    capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return ""
    nvidia, amd = "nvidia" in names, "radeon" in names or "amd" in names
    return "nvidia" if nvidia and not amd else "amd" if amd and not nvidia else ""


def game_settings(hdmi: int, hdr: bool, gpu: str) -> list[tuple[str, dict]]:
    """(category, settings) for bscpylgtv's set_settings. VRR & G-Sync for
    NVIDIA, FreeSync Premium for AMD. Video Range stays Auto: a PC sends
    full range, and the sheet's Limited assumes the source sends limited."""
    vrr, freesync = ("on", "off") if gpu == "nvidia" else ("off", "on")
    return [
        ("other", {"gameMode": {f"hdmi{hdmi}": "on"}}),                  # Game Optimizer on the input
        ("other", {f"uhdDeepColorHDMI{hdmi}": "on", "enableALLM": "on",
                   "gameOptimization": vrr, f"gameOptimizationHDMI{hdmi}": vrr,
                   "freesync": freesync, f"freesyncOLEDHDMI{hdmi}": freesync,
                   "inputOptimization": "on",                             # Prevent Input Delay: Boost
                   "gameGenre": "Standard", "blackStabilizer": 10, "whiteStabilizer": 10,
                   "lowLevelAdjustment": 0, "darkMode": "off", "blueLight": "off"}),
        ("aiPicture", {"ai_Picture": "off", "ai_Brightness": "off", "ai_Genre": "off"}),
        ("picture", {"energySaving": "off", "logoLuminanceAdjust": "off", "backlight": 100,
                     "contrast": 100 if hdr else 85, "brightness": 50 if hdr else 49,
                     "dynamicContrast": "off", "peakBrightness": "high" if hdr else "off",
                     "gamma": "medium",                                   # 2.2
                     "color": 50 if hdr else 55, "colorGamut": "auto",
                     "colorTemperature": -45,                             # Warm 45
                     "blackLevel": {k: "auto" for k in BLACK_LEVEL_KEYS},
                     "motionEyeCare": "off", "eyeComfortMode": "off", "sharpness": 0,
                     "superResolution": "off", "noiseReduction": "off", "mpegNoiseReduction": "off",
                     "smoothGradation": "off", "realCinema": "off", "motionProOLED": "off",
                     **({"hdrDynamicToneMapping": "HGIG"} if hdr else {})}),
    ]


def ensure_bscpylgtv(say) -> None:
    import importlib.util
    if importlib.util.find_spec("bscpylgtv") is None:
        say("Installing bscpylgtv (one time) ...")
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", "--use-pep517",
                        "--no-warn-script-location", "--disable-pip-version-check", "bscpylgtv"], check=True)


NAMES = {"gameMode": "Game Optimizer", "enableALLM": "ALLM", "inputOptimization": "Prevent Input Delay",
         "gameOptimization": "VRR & G-Sync", "freesync": "AMD FreeSync Premium", "gameGenre": "Game Genre",
         "blackStabilizer": "Black Stabiliser", "whiteStabilizer": "White Stabiliser",
         "lowLevelAdjustment": "Fine Tune Dark Areas", "darkMode": "Dark Room Mode", "blueLight": "Reduce Blue Light",
         "ai_Picture": "AI Picture Pro", "ai_Brightness": "AI Brightness", "ai_Genre": "AI Genre",
         "energySaving": "Energy Saving", "logoLuminanceAdjust": "Adjust Logo Brightness",
         "backlight": "OLED Pixel Brightness", "contrast": "Contrast", "brightness": "Black Level",
         "dynamicContrast": "Auto Dynamic Contrast", "peakBrightness": "Peak Brightness", "gamma": "Gamma",
         "color": "Colour Depth", "colorGamut": "Colour Gamut", "colorTemperature": "Colour Temperature",
         "blackLevel": "Video Range", "motionEyeCare": "Motion Eye Care", "eyeComfortMode": "Reduce Blue Light",
         "sharpness": "Sharpness", "superResolution": "Super Resolution", "noiseReduction": "Noise Reduction",
         "mpegNoiseReduction": "MPEG Noise Reduction", "smoothGradation": "Smooth Gradation",
         "realCinema": "Real Cinema", "motionProOLED": "OLED Motion", "truMotionMode": "TruMotion",
         "hdrDynamicToneMapping": "Dynamic Tone Mapping"}


def name(key: str) -> str:
    for prefix in ("uhdDeepColorHDMI", "gameOptimizationHDMI", "freesyncOLEDHDMI"):
        if key.startswith(prefix):
            return {"uhdDeepColorHDMI": "Ultra HD Deep Colour", "gameOptimizationHDMI": "VRR & G-Sync",
                    "freesyncOLEDHDMI": "AMD FreeSync Premium"}[prefix] + f" (HDMI {key[-1]})"
    return NAMES.get(key, key)


def same(wanted, got) -> bool:
    if isinstance(wanted, dict):
        return isinstance(got, dict) and all(same(v, got.get(k)) for k, v in wanted.items())
    return str(got).lower() == str(wanted).lower()


async def read(client, category: str, key: str):
    """One setting's value, or an Exception if the TV will not read it."""
    try:
        return (await client.get_system_settings(category, [key])).get("settings", {}).get(key)
    except Exception as exc:  # bscpylgtv raises on an error answer
        return exc


async def setup(client, hdmi: int, gpu: str, say, sleep=asyncio.sleep) -> list[str]:
    """Send everything, one setting per call (one bad key cannot sink the
    rest), then read each back. Returns the settings the TV shows differently."""
    say(f"Switching the TV to HDMI {hdmi} ...")
    await client.set_input(f"HDMI_{hdmi}")
    await sleep(3)
    try:
        await client.set_device_info(f"HDMI_{hdmi}", "pc", "PC")           # PC mode
    except Exception:
        await client.set_device_info_luna(f"HDMI_{hdmi}", "pc", "PC")
    await client.set_settings("other", {"gameMode": {f"hdmi{hdmi}": "on"}})
    await sleep(3)
    mode = await read(client, "picture", "pictureMode")
    if mode not in ("game", "hdrGame"):
        await client.set_current_picture_mode("game")
        await sleep(2)
        mode = await read(client, "picture", "pictureMode")
    hdr = mode == "hdrGame"
    say(f"Picture mode: {mode}. Sending the {'HDR' if hdr else 'SDR'} gaming settings ...")
    sets = [(category, {key: value}) for category, values in game_settings(hdmi, hdr, gpu)
            for key, value in values.items()]
    for category, values in sets:
        await client.set_settings(category, values)
    await client.set_settings("picture", {"truMotionMode": "off"}, True)  # needs current_app
    await client.enable_tpc_or_gsr("tpc", False)                           # OLED auto-dimming off
    await client.enable_tpc_or_gsr("gsr", False)
    await sleep(2)
    missed = [] if mode in ("game", "hdrGame") else [f"Game Optimizer (the TV is in {mode})"]
    unconfirmed = []
    for category, values in sets + [("picture", {"truMotionMode": "off"})]:
        (key, value), = values.items()
        got = await read(client, category, key)
        if isinstance(got, Exception):
            unconfirmed.append(name(key))
        elif not same(value, got):
            missed.append(f"{name(key)} (the TV has {json.dumps(got)})")
    total = len(sets) + 1
    say(f"Confirmed on the TV: {total - len(unconfirmed) - len(missed)} of {total} settings.")
    if unconfirmed:
        say("Sent, but the TV does not let a PC read these back, so check them once on the TV: "
            + ", ".join(dict.fromkeys(unconfirmed)) + ".")
    return missed


def run(ip: str, key_file: str, hdmi: int, say, ask_input=input, vendor=gpu_vendor,
        client_key: str | None = None) -> list[str]:
    """bscpylgtv pairs itself, as bscpylgtvcommand does (the TV asks once to
    allow it), and keeps its key in key_file."""
    from .app import choose_number
    gpu = vendor()
    if not gpu:
        for number, (_key, name) in enumerate(GPU_VENDORS, 1):
            say(f"  {number}. {name}")
        gpu = GPU_VENDORS[choose_number(f"Which graphics card drives HDMI {hdmi}? Type its number: ",
                                        len(GPU_VENDORS), ask_input)][0]
    say(f"Graphics: {dict(GPU_VENDORS)[gpu]}")
    ensure_bscpylgtv(say)
    from bscpylgtv import WebOsClient

    async def main():
        client = await WebOsClient.create(ip, key_file_path=key_file, client_key=client_key,
                                          ping_interval=None, states=[])
        if not client.client_key:
            say("The TV will ask to allow a connection (first time only): accept it with the remote.")
        await client.connect()
        try:
            return await setup(client, hdmi, gpu, say)
        finally:
            await client.disconnect()
    return asyncio.run(main())

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


def game_settings(hdmi: int, hdr: bool, gpu: str) -> dict[str, dict]:
    """The settings per route, as bscpylgtv's own G2 preset scripts send them
    (docs/guides/setting_presets): picture settings on the public route
    (set_system_settings), Game Optimizer ("other") and AI settings on the
    internal route (set_settings) followed by ENTER. VRR & G-Sync for
    NVIDIA, FreeSync Premium for AMD. Video Range stays Auto (a PC sends
    full range)."""
    vrr, freesync = ("on", "off") if gpu == "nvidia" else ("off", "on")
    picture = {"energySaving": "off", "backlight": "100", "contrast": "100" if hdr else "85",
               "brightness": "50" if hdr else "49", "dynamicContrast": "off",
               "peakBrightness": "high" if hdr else "off", "gamma": "medium",          # 2.2
               "motionEyeCare": "off", "color": "50" if hdr else "55", "colorGamut": "auto",
               "colorTemperature": "-45",                                              # Warm 45
               "sharpness": "0", "superResolution": "off", "noiseReduction": "off",
               "mpegNoiseReduction": "off", "smoothGradation": "off", "realCinema": "off",
               "blackLevel": {k: "auto" for k in BLACK_LEVEL_KEYS}}
    if hdr:
        picture["hdrDynamicToneMapping"] = "HGIG"
    return {
        "picture": picture,
        # The public route refuses these three on a G2; the internal route takes them.
        "picture_internal": {"logoLuminanceAdjust": "off", "eyeComfortMode": "off", "motionProOLED": "off"},
        "other": {"gameMode": {f"hdmi{hdmi}": "on"}, f"uhdDeepColorHDMI{hdmi}": "on", "enableALLM": "on",
                  "gameOptimization": vrr, f"gameOptimizationHDMI{hdmi}": vrr,
                  "freesync": freesync, f"freesyncOLEDHDMI{hdmi}": freesync,
                  "inputOptimization": "on",                                           # Prevent Input Delay: Boost
                  "gameGenre": "Standard", "blackStabilizer": 10, "whiteStabilizer": 10,
                  "lowLevelAdjustment": 0, "darkMode": "off", "blueLight": "off"},
        "aiPicture": {"ai_Picture": "off", "ai_Brightness": "off", "ai_Genre": "off"},
    }


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


async def internal(client, category: str, settings: dict, sleep) -> None:
    """The internal route, then ENTER, as bscpylgtv's G2 scripts do."""
    await client.set_settings(category=category, settings=settings)
    await sleep(2)
    await client.button(name="ENTER")
    await sleep(1)


async def setup(client, hdmi: int, gpu: str, say, sleep=asyncio.sleep) -> list[str]:
    """Send everything; return the picture settings the TV shows differently."""
    say(f"Switching the TV to HDMI {hdmi} ...")
    await client.set_input(f"HDMI_{hdmi}")
    await sleep(3)
    try:
        await client.set_device_info(f"HDMI_{hdmi}", "pc", "PC")                 # PC mode
    except Exception:
        await client.set_device_info_luna(f"HDMI_{hdmi}", "pc", "PC")
        await client.button(name="ENTER")
    await sleep(2)
    mode = await read(client, "picture", "pictureMode")
    if mode not in ("game", "hdrGame"):
        await internal(client, "picture", {"pictureMode": "game"}, sleep)
        mode = await read(client, "picture", "pictureMode")
    hdr = mode == "hdrGame"
    say(f"Picture mode: {mode}. Sending the {'HDR' if hdr else 'SDR'} gaming settings ...")
    sets = game_settings(hdmi, hdr, gpu)
    refused = []
    try:
        await client.set_system_settings(category="picture", settings=sets["picture"])
    except Exception:                  # one key refused sinks the batch: send them one by one
        for key, value in sets["picture"].items():
            try:
                await client.set_system_settings(category="picture", settings={key: value})
            except Exception as exc:
                refused.append(f"{name(key)} ({exc})")
    await sleep(1)
    await client.set_system_settings(category="picture", settings={"truMotionMode": "off"}, current_app=True)
    await sleep(1)
    for category, key in (("picture", "picture_internal"), ("other", "other"), ("aiPicture", "aiPicture")):
        await internal(client, category, sets[key], sleep)
    await client.enable_tpc_or_gsr("tpc", False)                                   # OLED auto-dimming off
    await sleep(1)
    await client.enable_tpc_or_gsr("gsr", False)
    await sleep(2)
    missed = refused + ([] if mode in ("game", "hdrGame") else [f"Game Optimizer (the TV is in {mode})"])
    for key, value in {**sets["picture"], "truMotionMode": "off"}.items():
        got = await read(client, "picture", key)
        if not isinstance(got, Exception) and not same(value, got):
            missed.append(f"{name(key)} (the TV has {json.dumps(got)})")
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

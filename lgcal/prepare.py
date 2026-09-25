"""The PGenerator-Plus wizard's preparation before an SDR greyscale AutoCal
(webui-workspace.js meterAutoCalResetDdc), and the matching undo.

1. Reset the picture mode to factory, including white balance
   (/api/lg/picture-settings/reset, require_white_balance_reset).
2. Clear the DDC white-balance / 1D LUT baseline and require the TV to
   confirm and verify it.
3. SDR reference reset: identity BT.709 3D LUT, 1D LUT and 3x3 matrix, so
   nothing from an earlier (HDR) calibration remains.

The wizard then lets the user dial the OLED brightness back to taste; here
the brightness read before the reset is simply put back.
"""
from __future__ import annotations

import time

PANEL_KEYS = ["backlight", "oledLight", "oledPixelBrightness"]
GREY_KEYS = ["pictureMode", "ddc_layout", "whiteBalanceMethod", "whiteBalanceIre", "whiteBalancePoint",
             "whiteBalanceRed", "whiteBalanceGreen", "whiteBalanceBlue", "adjustingLuminance"]


def retry(call, attempts: int = 3, sleep=time.sleep) -> dict:
    """The wizard's 3 attempts with 1.2 s, 2.4 s back-off."""
    result: dict = {}
    for attempt in range(1, attempts + 1):
        result = call()
        if result.get("status") == "ok":
            return result
        if attempt < attempts:
            sleep(1.2 * attempt)
    return result


def panel_light(picture: dict) -> tuple[str, float] | None:
    for key in PANEL_KEYS:
        try:
            return key, float(picture[key])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def clear_calibration(lg, mode: str, say, sleep=time.sleep) -> None:
    """Steps 2 and 3: the TV's white balance and LUTs back to neutral."""
    zero = [0] * 26
    result = retry(lambda: lg.picture_settings_set({
        "settings": {"whiteBalanceMethod": "22", "whiteBalanceIre": "109", "ddc_layout": "sdr26",
                     "whiteBalanceRed": zero, "whiteBalanceGreen": zero, "whiteBalanceBlue": zero,
                     "adjustingLuminance": zero},
        "picture_mode": mode, "reset_ddc_baseline": True, "force_ddc_white_balance": True,
        "lg_autocal_sdr_1d_dpg_upload_enabled": True, "helper_timeout": 170,
        "readback_keys": GREY_KEYS + PANEL_KEYS}), sleep=sleep)
    if result.get("status") != "ok":
        raise RuntimeError("Could not clear the TV's calibration data: " + (result.get("message") or "no answer"))
    if result.get("ddc_baseline_reset") is not True or result.get("ddc_1d_lut") is not True:
        raise RuntimeError("The TV did not confirm the 1D LUT baseline reset.")
    if result.get("ddc_reset_verified") is not True:
        raise RuntimeError("The TV's 1D LUT readback did not verify the reset.")
    result = retry(lambda: lg.sdr_calman_reset({"picture_mode": mode, "ddc_layout": "sdr26",
                                                "helper_timeout": 170}), sleep=sleep)
    if result.get("status") != "ok":
        raise RuntimeError("SDR calibration reset failed: " + (result.get("message") or "no answer"))


def prepare(lg, mode: str, mode_name: str, say, sleep=time.sleep, factory_reset: bool = True) -> None:
    if not factory_reset:
        say(f"Clearing the calibration data of {mode_name} first (other picture settings kept).")
        clear_calibration(lg, mode, say, sleep)
        return
    before = lg.picture_settings({"keys": PANEL_KEYS, "picture_mode": mode})
    panel = panel_light(before.get("picture_settings") or {}) if before.get("status") == "ok" else None
    say(f"Resetting {mode_name} to factory first, as PGenerator does"
        + (" (your OLED brightness is kept)." if panel else "."))
    result = retry(lambda: lg.picture_reset({"picture_mode": mode, "signal_mode": "sdr",
                                             "require_white_balance_reset": True}), sleep=sleep)
    if result.get("status") != "ok":
        raise RuntimeError("Picture mode reset failed: " + (result.get("message") or "no answer"))
    clear_calibration(lg, mode, say, sleep)
    if panel:
        key, value = panel
        after = lg.picture_settings({"keys": [key], "picture_mode": mode})
        current = (after.get("picture_settings") or {}).get(key)
        try:
            unchanged = abs(float(current) - value) < 0.1
        except (TypeError, ValueError):
            unchanged = False
        if not unchanged:
            write = lg.picture_settings_set({"settings": {key: int(value)}, "readback_keys": [key],
                                             "picture_mode": mode})
            if write.get("status") != "ok":
                # The wizard retries a panel-light write without the mode.
                write = lg.picture_settings_set({"settings": {key: int(value)}, "readback_keys": [key]})
            if write.get("status") != "ok":
                say(f"Could not put the OLED brightness back to {int(value)}; it is at the factory value.")

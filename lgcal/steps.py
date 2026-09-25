"""Build the worker config the PGenerator-Plus dashboard would send.

Ported from webui-workspace.js (meterBuildLgAutoCalSteps and the
/api/meter/lg-autocal request body) for the one path the PC supports:
SDR, RGB full range, 8 bits per channel.
"""
from __future__ import annotations

import math

# webui-app.js METER_LG_GREY_AUTOCAL_26_SLOTS_FULL: the full-range ladder has
# no super-white slots, and 100% is the separate white-reference step.
FULL_RANGE_SLOTS = [2.3, 3, 4, 5, 7, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55,
                    60, 65, 70, 75, 80, 85, 90, 95]
# Order the dashboard sends: white reference, black, then the worker's pass.
SEND_ORDER = [0, 50, 25, 75, 95, 90, 85, 80, 70, 65, 60, 55, 45, 40, 35, 30,
              20, 15, 10, 7, 5, 4, 3, 2.3]
INPUT_MAX = 255
TARGET_GAMMAS = ("bt1886", "2.2", "2.4", "srgb")
PICTURE_MODES = ("expert1", "expert2", "cinema", "filmMaker", "game", "normal",
                 "eco", "sports", "vivid", "personalized")


def js_round(value: float) -> int:
    """JavaScript Math.round (half rounds up)."""
    return math.floor(value + 0.5)


def format_percent(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def code_for_slot(slot: float) -> int:
    return max(0, min(INPUT_MAX, js_round(slot / 100 * 255)))


def stimulus_for_code(code: int) -> float:
    return code / 255 * 100


def target_yn(stimulus: float, target_gamma: str) -> float:
    v = stimulus / 100
    if v <= 0:
        return 0.0
    if target_gamma == "srgb":
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return v ** (2.2 if target_gamma == "2.2" else 2.4)


def _step(slot: float, code: int, target_gamma: str) -> dict:
    stimulus = stimulus_for_code(code)
    return {
        "ire": slot,
        "stimulus": stimulus,
        "signal_r_pct": stimulus,
        "signal_g_pct": stimulus,
        "signal_b_pct": stimulus,
        "r": code, "g": code, "b": code,
        "name": format_percent(slot) + "%",
        "series_type": "greyscale",
        "autocal_code": code,
        "input_max": INPUT_MAX,
        "target_x": 0.3127,
        "target_y": 0.3290,
        "target_Yn": target_yn(stimulus, target_gamma),
        "preview_r": code, "preview_g": code, "preview_b": code,
        "series_mode": "lg-autocal-26",
        "ddc_slot_locked": True,
        "autocal_slot_locked": True,
    }


def build_steps(target_gamma: str) -> list[dict]:
    white = _step(100, INPUT_MAX, target_gamma)
    white.update(read_delay_ms=3000, autocal_white_reference=True,
                 autocal_order_ire=100, autocal_target_label="100% full peak")
    by_slot = {slot: _step(slot, code_for_slot(slot), target_gamma) for slot in FULL_RANGE_SLOTS}
    black = _step(0, 0, target_gamma)
    black.update(autocal_slot_locked=False, autocal_read_only=True)
    by_slot[0] = black
    return [white] + [by_slot[slot] for slot in SEND_ORDER]


def build_config(settings: dict, white_luminance: float | None = None) -> dict:
    gamma = str(settings.get("target_gamma", "2.2")).lower()
    if gamma not in TARGET_GAMMAS:
        raise ValueError(f"target_gamma must be one of {', '.join(TARGET_GAMMAS)}")
    mode = settings.get("picture_mode", "expert1")
    if mode not in PICTURE_MODES:
        raise ValueError(f"picture_mode must be one of {', '.join(PICTURE_MODES)}")
    meter = settings.get("meter", {})
    white_y = float(white_luminance) if white_luminance and white_luminance > 0 else 100.0
    white_y = max(10.0, min(10000.0, white_y))
    return {
        "type": "greyscale",
        "points": 26,
        "display_type": meter.get("display_type", "oled"),
        "delay_ms": int(settings.get("delay_ms", 1800)),
        "patch_size": int(settings.get("patch_size", 10)),
        # 2 = full range. The PC draws 0-255 RGB; set the TV's HDMI Black
        # Level to match (see README).
        "signal_range": "2",
        "pattern_signal_range": "2",
        "transport_signal_range": "2",
        "color_format": "0",
        "colorimetry": "0",
        "primaries": "0",
        # Windows draws 8 bits per channel. Without this the worker assumes a
        # 10-bit link and drives its 10-bit headroom ladder.
        "max_bpc": 8,
        "lg_greyscale_21": False,
        "lg_autocal_26": True,
        "lg_autocal_26_full_ddc_spine": True,
        "lg_autocal_26_anchor_predrive": False,
        "lg_extended_sdr_16_255": False,
        "patch_insert": False,
        "patch_insert_patch_code": 0,
        "patch_insert_patch_input_max": 255,
        "patch_insert_time_code": 0,
        "patch_insert_time_input_max": 255,
        "target_delta_e": float(settings.get("target_delta_e", 0.5)),
        "delta_e_formula": "deitp",
        # The dashboard captures the current 100% white before starting and
        # sends it as the luminance reference (10..10000 cd/m2, default 100).
        "target_luminance": white_y,
        "setup_luminance_reference": white_y,
        "headroom_target_luminance": white_y,
        "target_gamma": gamma,
        "target_white": {"x": 0.3127, "y": 0.3290},
        "picture_mode": mode,
        "force_ddc_white_balance": True,
        "lg_autocal_sdr_1d_dpg_upload_enabled": True,
        "lg_autocal_sdr_1d_dpg_mode": True,
        "restore_factory_levels": False,
        "reset_ddc_baseline": False,
        "post_commit_polish": bool(settings.get("post_commit_polish", True)),
        "post_commit_verify": bool(settings.get("post_commit_verify", False)),
        "post_commit_body_verify": bool(settings.get("post_commit_verify", False)),
        "post_commit_final_all_level_verify": bool(settings.get("post_commit_verify", False)),
        "post_commit_final_top_window": bool(settings.get("post_commit_verify", False)),
        "refresh_rate": settings.get("refresh_rate"),
        "require_device_ready": False,
        "max_iterations": 36,
        "headroom_max_iterations": 60,
        "max_polish_iterations": 16,
        "precision_polish_iterations": 18,
        "low_light": {"enabled": False, "mode": "off", "trigger": 0},
        "observer": "1931_2",
        "signal_mode": "sdr",
        "max_luma": 1000,
        "steps": build_steps(gamma),
    }

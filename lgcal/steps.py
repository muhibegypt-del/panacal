"""Build the worker config the PGenerator-Plus dashboard would send.

Ported from webui-workspace.js (meterBuildLgAutoCalSteps and the
/api/meter/lg-autocal request body) for what a PC can send: SDR RGB,
8 bits per channel, full or limited range (the launcher measures which one
the TV is expecting).
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
# The worker's own ladder below 10% (meter_lg_autocal.pl @sdr26_labels).
WORKER_DARK_SLOTS = (2.3, 3, 4, 5, 7, 10)
# Dimmest patch the meter is trusted to steer the LUT. A Spyder5 tracked
# the TV at 0.35 cd/m2 but not at 0.16 (its readings fell while the TV was
# driven brighter), so anything dimmer is left to the curve measured above.
METER_FLOOR = 0.3
PICTURE_MODES = ("expert1", "expert2", "cinema", "filmMaker", "game", "normal",
                 "eco", "sports", "vivid", "personalized")


def js_round(value: float) -> int:
    """JavaScript Math.round (half rounds up)."""
    return math.floor(value + 0.5)


def format_percent(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def code_for_slot(slot: float, limited: bool = False) -> int:
    """meterBuildLgAutoCalSteps, 8-bit full or 8-bit limited."""
    if limited:
        return max(16, min(235, js_round(16 + slot / 100 * 219)))
    return max(0, min(INPUT_MAX, js_round(slot / 100 * 255)))


def stimulus_for_code(code: int, limited: bool = False) -> float:
    if limited:
        return (code - 16) * 100 / 219
    return code / 255 * 100


def target_yn(stimulus: float, target_gamma: str) -> float:
    v = stimulus / 100
    if v <= 0:
        return 0.0
    if target_gamma == "srgb":
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return v ** (2.2 if target_gamma == "2.2" else 2.4)


def _step(slot: float, code: int, target_gamma: str, limited: bool) -> dict:
    stimulus = stimulus_for_code(code, limited)
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


def build_steps(target_gamma: str, limited: bool = False) -> list[dict]:
    """RGB limited uses the same 24-anchor shape as full range (the worker
    keeps super-white slots for YCbCr only), with codes 16-235."""
    white = _step(100, 235 if limited else INPUT_MAX, target_gamma, limited)
    white.update(read_delay_ms=3000, autocal_white_reference=True, autocal_order_ire=100,
                 autocal_target_label="100% peak" if limited else "100% full peak")
    by_slot = {slot: _step(slot, code_for_slot(slot, limited), target_gamma, limited)
               for slot in FULL_RANGE_SLOTS}
    black = _step(0, 16 if limited else 0, target_gamma, limited)
    black.update(autocal_slot_locked=False, autocal_read_only=True)
    by_slot[0] = black
    return [white] + [by_slot[slot] for slot in SEND_ORDER]


def slot_luminance(slot: float, white: float, target_gamma: str, limited: bool = False) -> float:
    """Target cd/m2 of a ladder slot, as the worker computes it."""
    return white * target_yn(stimulus_for_code(code_for_slot(slot, limited), limited), target_gamma)


def dark_threshold(white: float, target_gamma: str, limited: bool = False, floor: float = METER_FLOOR) -> float:
    """Lowest worker slot whose target is bright enough for the meter.

    Slots below it become the worker's "low" tier. The worker caps the
    threshold at 10%, so 10% is always calibrated."""
    for slot in WORKER_DARK_SLOTS:
        if slot_luminance(slot, white, target_gamma, limited) >= floor:
            return float(slot)
    return float(WORKER_DARK_SLOTS[-1])


def dark_overrides(white: float, target_gamma: str, limited: bool = False, floor: float = METER_FLOOR) -> dict:
    """Worker settings that stop it chasing unreadable dark patches.

    Low-tier anchors get one iteration: the worker reads once, and its
    final-state restore puts back the value seeded from the calibrated
    curve above (the solver's move from that reading is discarded)."""
    threshold = dark_threshold(white, target_gamma, limited, floor)
    if threshold <= WORKER_DARK_SLOTS[0]:
        return {}
    return {"lg_autocal_sdr26_dpg_low_ire_threshold": threshold, "lg_autocal_sdr26_dpg_inner_iters_low": 1}


def build_config(settings: dict, white_luminance: float | None = None, *, limited: bool = False,
                 picture_mode: str | None = None) -> dict:
    gamma = str(settings.get("target_gamma", "2.2")).lower()
    if gamma not in TARGET_GAMMAS:
        raise ValueError(f"target_gamma must be one of {', '.join(TARGET_GAMMAS)}")
    mode = picture_mode or settings.get("picture_mode") or "expert1"
    if mode not in PICTURE_MODES:
        raise ValueError(f"picture_mode must be one of {', '.join(PICTURE_MODES)}")
    meter = settings.get("meter", {})
    white_y = float(white_luminance) if white_luminance and white_luminance > 0 else 100.0
    white_y = max(10.0, min(10000.0, white_y))
    range_code = "1" if limited else "2"
    return {
        "type": "greyscale",
        "points": 26,
        "display_type": meter.get("display_type", "oled"),
        # The worker raises SDR reads to at least 1800 ms itself.
        "delay_ms": 1800,
        "patch_size": int(settings.get("patch_size", 10)),
        # "2" full range, "1" limited: chosen from what the TV was measured
        # to expect, so its Black Level setting does not have to be changed.
        "signal_range": range_code,
        "pattern_signal_range": range_code,
        "transport_signal_range": range_code,
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
        # Pattern insertion as the dashboard sends it for OLED (on; 1 s 10%
        # flash per reading, 5 s 25% flash every 45 s). The worker itself
        # switches it off on the 8-bit path (apply_lg_autocal_26_default_modes)
        # and uses it only with its 10-bit headroom ladder.
        "patch_insert": bool(settings.get("pattern_insertion", True)),
        "patch_insert_patch_enabled": True,
        "patch_insert_patch_every": 1,
        "patch_insert_patch_duration_ms": 1000,
        "patch_insert_patch_level": 10,
        "patch_insert_time_enabled": True,
        "patch_insert_time_frequency_ms": 45000,
        "patch_insert_time_duration_ms": 5000,
        "patch_insert_time_level": 25,
        "pattern_delay_ms": 0,
        "patch_insert_patch_code": code_for_slot(10, limited),
        "patch_insert_patch_input_max": INPUT_MAX,
        "patch_insert_time_code": code_for_slot(25, limited),
        "patch_insert_time_input_max": INPUT_MAX,
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
        "steps": build_steps(gamma, limited),
        **dark_overrides(white_y, gamma, limited, float(meter.get("floor_cd_m2", METER_FLOOR))),
    }

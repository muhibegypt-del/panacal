"""settings.json: optional overrides, checked before anything touches the TV.

Every value is validated here, at the boundary, so a typo stops the run with
a clear message instead of failing half way through a calibration (after the
picture mode has already been reset). Unknown keys are reported, not ignored.
"""
from __future__ import annotations

import difflib
import json
from pathlib import Path

from .lg import valid_ipv4
from .steps import PICTURE_MODES, TARGET_GAMMAS

DEFAULTS = {"target_gamma": "bt1886", "target_delta_e": 0.5, "patch_size": 10, "api_port": 8765,
            "reset_picture_mode": True, "picture_mode": "", "tv_ip": "", "perl": "", "argyll_bin": "",
            "pattern_insertion": True, "meter": {}}
METER_DEFAULTS = {"ccss": "", "spotread": "", "args": ["-e"], "display_type": "oled", "synthetic_black": True,
                  "floor_cd_m2": 0.3}
IGNORED = {"_comment"}


def _number(value, name, low, high, errors, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{name} must be a number between {low} and {high} (got {value!r})")
        return None
    if integer and value != int(value):
        errors.append(f"{name} must be a whole number (got {value!r})")
        return None
    if not low <= value <= high:
        errors.append(f"{name} must be between {low} and {high} (got {value!r})")
        return None
    return int(value) if integer else float(value)


def _bool(value, name, errors):
    if not isinstance(value, bool):
        errors.append(f"{name} must be true or false (got {value!r})")
        return None
    return value


def _path(value, name, errors, directory=False):
    if value in ("", None):
        return ""
    if not isinstance(value, str):
        errors.append(f"{name} must be a path in quotes (got {value!r})")
        return ""
    path = Path(value)
    if not (path.is_dir() if directory else path.is_file()):
        errors.append(f"{name}: {value} does not exist")
        return ""
    return value


def _unknown(keys, known, prefix, warnings):
    for key in keys:
        if key in known or key in IGNORED:
            continue
        guess = difflib.get_close_matches(key, list(known), n=1)
        warnings.append(f"Unknown setting '{prefix}{key}' ignored" + (f" (did you mean '{prefix}{guess[0]}'?)"
                                                                     if guess else ""))


def validate(raw) -> tuple[dict, list[str]]:
    """Return (settings with defaults, warnings); raise ValueError listing
    every problem at once."""
    if not isinstance(raw, dict):
        raise ValueError("settings.json must contain a JSON object ({ ... })")
    errors: list[str] = []
    warnings: list[str] = []
    _unknown(raw, DEFAULTS, "", warnings)
    merged = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    out: dict = {}

    gamma = str(merged["target_gamma"]).lower()
    if gamma not in TARGET_GAMMAS:
        errors.append(f"target_gamma must be one of {', '.join(TARGET_GAMMAS)} (got {merged['target_gamma']!r})")
    out["target_gamma"] = gamma
    out["target_delta_e"] = _number(merged["target_delta_e"], "target_delta_e", 0.1, 10, errors)
    out["patch_size"] = _number(merged["patch_size"], "patch_size", 1, 100, errors, integer=True)
    out["api_port"] = _number(merged["api_port"], "api_port", 1024, 65535, errors, integer=True)
    out["reset_picture_mode"] = _bool(merged["reset_picture_mode"], "reset_picture_mode", errors)
    out["pattern_insertion"] = _bool(merged["pattern_insertion"], "pattern_insertion", errors)

    mode = merged["picture_mode"] or ""
    if mode and mode not in PICTURE_MODES:
        errors.append(f"picture_mode must be empty or one of {', '.join(PICTURE_MODES)} (got {mode!r})")
    out["picture_mode"] = mode
    ip = merged["tv_ip"] or ""
    if ip and not valid_ipv4(ip):
        errors.append(f"tv_ip must be an IPv4 address like 192.168.1.60 (got {ip!r})")
    out["tv_ip"] = ip
    out["perl"] = _path(merged["perl"], "perl", errors)
    out["argyll_bin"] = _path(merged["argyll_bin"], "argyll_bin", errors, directory=True)

    meter_raw = merged["meter"]
    if not isinstance(meter_raw, dict):
        errors.append("meter must be an object ({ ... })")
        meter_raw = {}
    _unknown(meter_raw, METER_DEFAULTS, "meter.", warnings)
    meter = {**METER_DEFAULTS, **{k: v for k, v in meter_raw.items() if k in METER_DEFAULTS}}
    meter["ccss"] = _path(meter["ccss"], "meter.ccss", errors)
    meter["spotread"] = _path(meter["spotread"], "meter.spotread", errors)
    if not isinstance(meter["args"], list) or not all(isinstance(a, str) for a in meter["args"]):
        errors.append(f"meter.args must be a list of strings like [\"-e\"] (got {meter['args']!r})")
    if not isinstance(meter["display_type"], str) or not meter["display_type"]:
        errors.append("meter.display_type must be a non-empty string")
    meter["synthetic_black"] = _bool(meter["synthetic_black"], "meter.synthetic_black", errors)
    meter["floor_cd_m2"] = _number(meter["floor_cd_m2"], "meter.floor_cd_m2", 0, 5, errors)
    out["meter"] = meter

    if errors:
        raise ValueError("settings.json has problems:\n  - " + "\n  - ".join(errors))
    return out, warnings


def load(path: Path) -> tuple[dict, list[str]]:
    """Defaults when the file is absent; ValueError (with the reason) if it
    exists but cannot be used."""
    if not path.is_file():
        return validate({})
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from None
    return validate(raw)

"""The colour stage: the author's 3D LUT worker (meter_lg_3d_autocal.pl).

LG keeps its factory BT.709 colour conversion in the calibration data that
the greyscale preparation clears (the SDR reference reset writes identity),
so after a greyscale run the panel shows its native wide gamut. The colour
worker measures white, red, green, blue and black, and uploads a 3D LUT that
maps BT.709 onto the measured panel. Its grey axis is identity, so the
greyscale 1D LUT keeps owning greys; the finished greyscale table is still
committed again afterwards, as the author's full workflow does.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .meter import read_with_recovery
from .signal import delta_e_itp
from .steps import TARGET_GAMMAS

# BT.709 primaries and secondaries as (name, r, g, b) at full signal.
COLOUR_PATCHES = (("Red", 1, 0, 0), ("Green", 0, 1, 0), ("Blue", 0, 0, 1),
                  ("Cyan", 0, 1, 1), ("Magenta", 1, 0, 1), ("Yellow", 1, 1, 0))
# BT.709 linear RGB -> XYZ, D65 white at Y=1.
M709 = ((0.4124564, 0.3575761, 0.1804375),
        (0.2126729, 0.7151522, 0.0721750),
        (0.0193339, 0.1191920, 0.9503041))


def build_colour_config(settings: dict, *, limited: bool, picture_mode: str, lut_dir: Path,
                        run_id: str) -> dict:
    """The body the dashboard posts to /api/meter/lg-3d-autocal/start for an
    SDR 'matrix' run, with the PC's 8-bit patterns and measured range."""
    gamma = str(settings.get("target_gamma", "bt1886")).lower()
    if gamma not in TARGET_GAMMAS:
        raise ValueError(f"target_gamma must be one of {', '.join(TARGET_GAMMAS)}")
    range_code = "1" if limited else "2"
    meter = settings.get("meter") or {}
    return {
        "method": "matrix",
        "type": "lg-3d-lut",
        "display_type": meter.get("display_type", "oled"),
        "delay_ms": 1800,
        "patch_size": int(settings.get("patch_size", 10)),
        "signal_range": range_code,
        "pattern_signal_range": range_code,
        "transport_signal_range": range_code,
        "target_gamut": "bt709",
        "target_gamma": gamma,
        # The greyscale stage calibrated to this curve; the cube is solved on it.
        "greyscale_target_gamma": gamma,
        "picture_mode": picture_mode,
        "signal_mode": "sdr",
        "requested_signal_mode": "sdr",
        "require_device_ready": False,
        "observer": "1931_2",
        "low_light": {"enabled": False, "mode": "off", "trigger": 0},
        "patch_insert": False,
        "pattern_delay_ms": 0,
        "max_bpc": 8,
        "solve_cube_size": 17,
        # Greys stay identity in the cube: the 1D LUT owns them.
        "include_greyscale": 0,
        "upload": True,
        "post_check": False,
        "lut_dir": Path(lut_dir).as_posix(),
        "run_id": run_id,
    }


def find_saved_greyscale(sessions: Path, picture_mode: str) -> tuple[list[int] | None, Path | None]:
    """The greyscale table last committed for this picture mode by a
    finished run: the dark-end-extended table when there is one (that is
    what the TV received), else the worker's final table."""
    candidates = sorted((p for p in Path(sessions).glob("*") if p.is_dir()), reverse=True)
    for directory in candidates:
        try:
            config = json.loads((directory / "worker_config.json").read_text(encoding="utf-8"))
            state = json.loads((directory / "worker_state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if config.get("picture_mode") != picture_mode or state.get("status") != "complete":
            continue
        if not state.get("sdr_1d_dpg_single_socket_commit"):
            continue
        table = state.get("sdr_1d_dpg_data")
        dark_end = directory / "dark_end.json"
        if dark_end.exists():
            try:
                table = json.loads(dark_end.read_text(encoding="utf-8")).get("committed_table") or table
            except (OSError, ValueError):
                pass
        if isinstance(table, list) and len(table) == 3072:
            return [int(v) for v in table], directory
    return None, None


def commit_greyscale(lg, table: list[int], picture_mode: str) -> dict:
    """Commit a greyscale table the way the worker's single-socket commit
    does (CAL_START, 1D LUT, CAL_END on one connection)."""
    if not isinstance(table, list) or len(table) != 3072:
        raise ValueError("a greyscale table has 3072 values")
    result = lg.dpg_upload({"picture_mode": picture_mode, "ddc_layout": "sdr26", "signal_mode": "sdr",
                            "dpg_data": table, "keep_calibration_mode": False,
                            "calibration_mode_active": False, "helper_timeout": 90})
    committed = (result.get("status") == "ok"
                 and (result.get("cal_start_response") or {}).get("type") == "response"
                 and (result.get("cal_end_response") or {}).get("type") == "response")
    return {**result, "committed": committed}


def colour_target(white_y: float, r: float, g: float, b: float) -> tuple[float, float, float]:
    """BT.709 XYZ of a full-signal patch for a display whose white is white_y."""
    return tuple(white_y * sum(M709[row][c] * v for c, v in enumerate((r, g, b))) for row in range(3))


def colour_row(name: str, xyz, target) -> dict:
    X, Y, Z = xyz
    total = X + Y + Z
    tx, ty = target[0] / sum(target), target[1] / sum(target)
    return {"name": name, "Y": Y, "target_Y": target[1], "x": X / total if total else 0.0,
            "y": Y / total if total else 0.0, "target_x": tx, "target_y": ty, "de": delta_e_itp(xyz, target)}


def verify_colours(reader, white_y: float, limited: bool, say) -> dict:
    """Measure the six BT.709 colours at full signal against their targets."""
    top = 235 if limited else 255
    bottom = 16 if limited else 0
    rows = []
    for name, r, g, b in COLOUR_PATCHES:
        codes = [top if v else bottom for v in (r, g, b)]
        reader.pattern.show(*codes, reader.area)
        time.sleep(1.8 * reader.settle_scale)
        xyz = read_with_recovery(reader.meter, reader.log)
        rows.append(colour_row(name, xyz, colour_target(white_y, r, g, b)))
    say(f"{'Colour':>8}  {'Y cd/m2':>9}  {'target':>9}  {'x':>7}  {'y':>7}  {'target x,y':>15}  {'dE ITP':>6}")
    for row in rows:
        say(f"{row['name']:>8}  {row['Y']:9.2f}  {row['target_Y']:9.2f}  {row['x']:7.4f}  {row['y']:7.4f}  "
            f"{row['target_x']:7.4f},{row['target_y']:7.4f}  {row['de']:6.2f}")
    des = [row["de"] for row in rows]
    summary = {"rows": rows, "average_de": sum(des) / len(des), "max_de": max(des)}
    say(f"Colours: average dE ITP {summary['average_de']:.2f}, worst {summary['max_de']:.2f}")
    return summary


def colour_report(state: dict) -> tuple[bool, str]:
    """(finished and uploaded, one-line reason)."""
    if state.get("status") != "complete":
        return False, state.get("message") or "the colour worker stopped without a status"
    if not state.get("upload_verified"):
        return False, state.get("upload_message") or "the TV did not confirm the 3D LUT"
    return True, "The colour correction (3D LUT) is saved in the TV."

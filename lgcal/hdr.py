"""HDR10: the author's HDR20 greyscale and HDR colour stage, fed from a PC.

The flow is the dashboard's HDR Full AutoCal:
  1. the HDR reference reset (identity BT.2020 3D LUT, 1D LUT and matrix);
  2. the greyscale worker's HDR20 path: 20 levels, each calibrated on a
     2.2 curve against the measured peak while the TV is held in LG's
     calibration pass-through (calibration mode stays held at the end);
  3. the colour worker's HDR 'matrix' run, which inherits that session,
     uploads the BT.2020 3D LUT and finally LG's tone map for the measured
     peak together with the greyscale table (CAL_END).
Patterns come from madTPG, which sends a real HDR10 signal (lgcal.madtpg).
"""
from __future__ import annotations

import math
import time
from pathlib import Path

from .meter import read_with_recovery, xyz_record
from .signal import D65, delta_e_itp, xyz_from_xyy
from .steps import build_config

# webui-app.js: the HDR greyscale ladder (stored descending there) and its
# 10-bit full-range codes.
HDR_SLOTS = (100, 90, 80, 70, 60, 50, 45, 40, 35, 30, 25, 20, 15, 10, 7, 5, 4, 2.7, 2, 1.4)
HDR_CODES_10BIT_FULL = (1023, 921, 818, 716, 614, 512, 460, 409, 358, 307, 256, 205, 153, 102, 72, 51,
                        41, 28, 20, 14)
# meter_lg_autocal.pl @hdr20_idx_labels / @hdr20_idx_values.
HDR_INDEX_SLOTS = (1.4, 2, 2.7, 4, 5, 7, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 90, 100)
HDR_INDEXES = (14, 19, 28, 42, 51, 70, 103, 154, 206, 257, 308, 360, 411, 462, 514, 612, 715, 817, 920, 1023)
HDR_DARK_SLOTS = (1.4, 2, 2.7, 4, 5, 7, 10)
HDR_FIT_LADDER = (1.4, 2, 2.7, 4, 5, 7, 10, 15, 20, 25, 30)
# Picture modes LG accepts HDR calibration data for (pgenerator-lg's
# calibration picMode map; Standard, Vivid, Eco and Personalised are not).
HDR_PICTURE_MODES = ("hdrCinema", "hdrCinemaBright", "hdrFilmMaker", "hdrGame", "hdrTechnicolor")
HDR_MODE_NAMES = {"hdrCinema": "Cinema (HDR)", "hdrCinemaBright": "Cinema Home (HDR)",
                  "hdrFilmMaker": "Filmmaker (HDR)", "hdrGame": "Game Optimizer (HDR)",
                  "hdrTechnicolor": "Technicolor (HDR)"}
TARGET_GAMMA = 2.2    # the HDR20 path's calibration curve in LG's pass-through


def hdr_index(slot: float) -> int:
    """meter_lg_autocal.pl HDR20 slot -> 1D DPG index (linear in between)."""
    labels, values = HDR_INDEX_SLOTS, HDR_INDEXES
    if slot <= labels[0]:
        return values[0]
    for k in range(len(labels) - 1):
        if labels[k] <= slot <= labels[k + 1]:
            f = (slot - labels[k]) / (labels[k + 1] - labels[k])
            return int(values[k] + (values[k + 1] - values[k]) * f + 0.5)
    return values[-1]


def hdr_steps() -> list[dict]:
    """webui-workspace.js meterBuildLgAutoCalSteps, HDR10 branch, 10-bit full."""
    def step(slot, code):
        preview = round(code * 255 / 1023)
        return {"ire": slot, "stimulus": slot, "signal_r_pct": slot, "signal_g_pct": slot,
                "signal_b_pct": slot, "analysis_ire": slot, "target_ire": slot,
                "r": code, "g": code, "b": code, "name": f"{slot:g}%", "series_type": "greyscale",
                "autocal_code": code, "input_max": 1023, "preview_r": preview, "preview_g": preview,
                "preview_b": preview, "series_mode": "lg-autocal-26", "ddc_slot_locked": True,
                "autocal_slot_locked": True, "ddc_layout": "hdr20", "ddc_target_ire": slot,
                "ddc_array_ire": slot, "autocal_order_ire": slot}
    zero = {"ire": 0, "stimulus": 0, "signal_r_pct": 0, "signal_g_pct": 0, "signal_b_pct": 0,
            "r": 0, "g": 0, "b": 0, "input_max": 1023, "name": "0%", "series_type": "greyscale",
            "autocal_code": 0, "preview_r": 0, "preview_g": 0, "preview_b": 0,
            "series_mode": "lg-autocal-26", "autocal_slot_locked": False, "autocal_read_only": True}
    body = [step(s, c) for s, c in reversed(list(zip(HDR_SLOTS, HDR_CODES_10BIT_FULL)))]
    return [zero, *body]


def hdr_dark_threshold(peak: float, floor: float) -> float:
    """Lowest HDR20 slot bright enough for the meter on the 2.2 curve."""
    for slot in HDR_DARK_SLOTS:
        if peak * (slot / 100) ** TARGET_GAMMA >= floor:
            return float(slot)
    return float(HDR_DARK_SLOTS[-1])


def build_hdr_greyscale_config(settings: dict, peak: float, picture_mode: str, floor: float) -> dict:
    """The dashboard's HDR10 greyscale body (webui.pm routes signal_mode
    hdr10 to lg_autocal_hdr20_dpg_mode) for 10-bit full-range patterns."""
    config = build_config({**settings, "target_gamma": "2.2", "picture_mode": ""}, peak, limited=False,
                          picture_mode="expert1")
    for key in [k for k in config if k.startswith("lg_autocal_sdr26_")]:
        del config[key]
    config.pop("lg_autocal_sdr_1d_dpg_mode", None)
    threshold = hdr_dark_threshold(peak, floor) if floor > 0 else HDR_DARK_SLOTS[0]
    config.update({
        "signal_mode": "hdr10",
        "requested_signal_mode": "hdr10",
        "lg_autocal_hdr20_dpg_mode": True,
        "picture_mode": picture_mode,
        "max_bpc": 10,
        "signal_range": "2", "pattern_signal_range": "2", "transport_signal_range": "2",
        "target_gamma": "2.2",
        # Hold calibration mode for the colour stage, which uploads the
        # 3D LUT, the greyscale table and the tone map in one session.
        "full_workflow": True,
        "full_autocal_phase": "greyscale",
        "steps": hdr_steps(),
    })
    # HDR uses the panel's native peak: no 109% headroom reference.
    config.pop("headroom_target_luminance", None)
    if threshold > HDR_DARK_SLOTS[0]:
        config.update({"lg_autocal_hdr20_dpg_low_ire_threshold": threshold,
                       "lg_autocal_hdr20_dpg_inner_iters_low": 1,
                       "lg_autocal_hdr20_dpg_inner_iters_very_low": 1})
    return config


def build_hdr_colour_config(base: dict, peak: float, greyscale_table: list[int] | None) -> dict:
    """The dashboard's HDR 3D LUT body in the full workflow."""
    config = {**base,
              "signal_mode": "hdr10", "requested_signal_mode": "hdr10",
              "target_gamut": "bt2020", "target_gamma": "st2084", "greyscale_target_gamma": "2.2",
              "upload_command": "BT2020_3D_LUT_DATA", "get_command": "GET_3D_LUT_DATA",
              "max_bpc": 10,
              "signal_range": "2", "pattern_signal_range": "2", "transport_signal_range": "2",
              "full_workflow": True, "full_autocal_phase": "3d-lut",
              "full_workflow_peak_luminance": peak,
              # The HDR reference reset already wrote and verified the
              # identity BT.2020 3D LUT, as the wizard's preflight does.
              "skip_preprofile_unity_reset": True, "preflight_3d_lut_verified": True,
              "lg_autocal_hdr20_postcal_shadow_enable": 0}
    if greyscale_table is not None:
        config["full_workflow_dpg_data"] = greyscale_table
    return config


# --- verification (after the tone map; the TV is out of calibration mode) ----

PQ_M1, PQ_M2 = 2610 / 16384, 2523 / 4096 * 128
PQ_C1, PQ_C2, PQ_C3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32
VERIFY_SIGNALS = (5, 10, 15, 20, 25, 30, 40, 50, 60, 65, 70)
M2020 = ((0.6369580, 0.1446169, 0.1688810), (0.2627002, 0.6779981, 0.0593017),
         (0.0000000, 0.0280727, 1.0609851))
P3_PRIMARIES = ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060))
BT709_PRIMARIES = ((0.640, 0.330), (0.300, 0.600), (0.150, 0.060))
COLOUR_BASE_NITS = 100.0   # colour patches are checked well below any tone mapping


def pq_decode(signal: float) -> float:
    """ST 2084 signal (0..1) -> cd/m2."""
    if signal <= 0:
        return 0.0
    p = signal ** (1 / PQ_M2)
    return 10000 * (max(p - PQ_C1, 0.0) / (PQ_C2 - PQ_C3 * p)) ** (1 / PQ_M1)


def pq_encode(nits: float) -> float:
    if nits <= 0:
        return 0.0
    y = (min(nits, 10000.0) / 10000) ** PQ_M1
    return ((PQ_C1 + PQ_C2 * y) / (1 + PQ_C3 * y)) ** PQ_M2


def _inverse(m):
    det = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
           + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    return [[(m[(j + 1) % 3][(i + 1) % 3] * m[(j + 2) % 3][(i + 2) % 3]
              - m[(j + 1) % 3][(i + 2) % 3] * m[(j + 2) % 3][(i + 1) % 3]) / det for j in range(3)] for i in range(3)]


def _rgb_to_xyz(primaries):
    cols = [(x / y, 1.0, (1 - x - y) / y) for x, y in primaries]
    m = [[cols[c][r] for c in range(3)] for r in range(3)]
    white = (D65[0] / D65[1], 1.0, (1 - D65[0] - D65[1]) / D65[1])
    inv = _inverse(m)
    scale = [sum(inv[i][k] * white[k] for k in range(3)) for i in range(3)]
    return [[m[r][c] * scale[c] for c in range(3)] for r in range(3)]


M_P3 = _rgb_to_xyz(P3_PRIMARIES)
M_709 = _rgb_to_xyz(BT709_PRIMARIES)
M2020_INV = _inverse(M2020)


def colour_patches(matrix=None) -> list[tuple[str, tuple[float, float, float], tuple[float, float, float]]]:
    """(name, BT.2020 PQ signal 0..1 per channel, target XYZ) for BT.709
    primaries and secondaries inside the BT.2020 container, on a 100 cd/m2
    white basis. BT.709 is inside every WOLED panel's gamut (the P3 corners
    are not quite, so they would score the panel, not the calibration)."""
    matrix = matrix or M_709
    out = []
    for name, rgb in (("Red", (1, 0, 0)), ("Green", (0, 1, 0)), ("Blue", (0, 0, 1)),
                      ("Cyan", (0, 1, 1)), ("Magenta", (1, 0, 1)), ("Yellow", (1, 1, 0))):
        xyz = [COLOUR_BASE_NITS * sum(matrix[r][c] * rgb[c] for c in range(3)) for r in range(3)]
        lin2020 = [max(0.0, sum(M2020_INV[r][c] * xyz[c] for c in range(3))) for r in range(3)]
        out.append((name, tuple(pq_encode(v) for v in lin2020), tuple(xyz)))
    return out


def grey_row(signal_pct: float, xyz, peak: float, floor: float) -> dict:
    target_y = pq_decode(signal_pct / 100)
    target = xyz_from_xyy(*D65, target_y)
    record = xyz_record(xyz)
    return {"signal": signal_pct, "Y": xyz[1], "target_Y": target_y, "x": record["x"], "y": record["y"],
            "de": delta_e_itp(xyz, target), "below_floor": target_y < floor,
            # LG's tone map rolls off towards the peak; above half of it the
            # target is the TV's curve, not PQ.
            "tone_mapped": target_y > 0.5 * peak}


def show_signal(pattern, rgb01, area: float) -> None:
    pattern.show_code(*(v * 1023 for v in rgb01), 1023, area)


def verify_hdr(pattern, meter, log, area: float, peak: float, floor: float, say, settle: float = 2.0) -> dict:
    """Fresh measurement of the finished HDR calibration: PQ greyscale and
    BT.709 colours in the BT.2020 container, dE ITP (the metric made for HDR)."""
    say("")
    say("Verifying the HDR result (independent measurement, TV out of calibration mode) ...")
    rows = []
    for signal in VERIFY_SIGNALS:
        show_signal(pattern, (signal / 100,) * 3, area)
        time.sleep(settle)
        rows.append(grey_row(signal, read_with_recovery(meter, log), peak, floor))
    say(f"{'Signal':>6}  {'Y cd/m2':>9}  {'PQ target':>9}  {'x':>7}  {'y':>7}  {'dE ITP':>6}")
    for row in rows:
        mark = "  *" if row["below_floor"] else ("  (tone-mapped)" if row["tone_mapped"] else "")
        say(f"{row['signal']:>5}%  {row['Y']:9.3f}  {row['target_Y']:9.3f}  {row['x']:7.4f}  {row['y']:7.4f}  "
            f"{row['de']:6.2f}{mark}")
    scored = [r["de"] for r in rows if not r["below_floor"] and not r["tone_mapped"]]
    colours = []
    for name, signal, target in colour_patches():
        show_signal(pattern, signal, area)
        time.sleep(settle)
        xyz = read_with_recovery(meter, log)
        total = sum(xyz) or 1.0
        colours.append({"name": name, "Y": xyz[1], "target_Y": target[1], "x": xyz[0] / total,
                        "y": xyz[1] / total, "target_x": target[0] / sum(target),
                        "target_y": target[1] / sum(target), "de": delta_e_itp(xyz, target)})
    say(f"{'Colour':>9}  {'Y cd/m2':>9}  {'target':>9}  {'x':>7}  {'y':>7}  {'target x,y':>15}  {'dE ITP':>6}")
    for c in colours:
        say(f"{c['name']:>9}  {c['Y']:9.2f}  {c['target_Y']:9.2f}  {c['x']:7.4f}  {c['y']:7.4f}  "
            f"{c['target_x']:7.4f},{c['target_y']:7.4f}  {c['de']:6.2f}")
    summary = {"rows": rows, "colours": colours, "peak": peak,
               "average_de": sum(scored) / len(scored) if scored else math.nan,
               "max_de": max(scored) if scored else math.nan,
               "colour_average_de": sum(c["de"] for c in colours) / len(colours),
               "colour_max_de": max(c["de"] for c in colours)}
    say(f"Greyscale: average dE ITP {summary['average_de']:.2f}, worst {summary['max_de']:.2f}   "
        f"Colours: average {summary['colour_average_de']:.2f}, worst {summary['colour_max_de']:.2f}")
    say("* below what the meter reads reliably; (tone-mapped) above half the peak, where LG's tone map "
        "rolls off. Both are shown, not scored.")
    return summary

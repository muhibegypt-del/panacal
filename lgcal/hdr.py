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
# The TV's own brightness processing. LG's calibration mode bypasses it, so
# with it on, what you watch is not what was calibrated, and the check at the
# end measures it instead of the calibration. (setting key, name on the TV,
# values that leave the picture steady; HGIG is a fixed curve.) The author
# turns AI Picture and Energy Saving off before his Dolby Vision measurement.
STEADY_PICTURE = (("hdrDynamicToneMapping", "Dynamic Tone Mapping", ("off", "hgig")),
                  ("aiPicture", "AI Picture Pro", ("off",)),
                  ("energySaving", "Energy Saving", ("off",)))
DTM_MENU = "Settings > Picture > Advanced Settings > Brightness > Dynamic Tone Mapping"


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


def steady_picture(lg, picture_mode: str, say, log, timeout: float = 600, clock=time.monotonic,
                   sleep=time.sleep) -> list[str]:
    """Turn off Dynamic Tone Mapping, AI Picture Pro and Energy Saving for
    the HDR mode being calibrated. Returns the names of those turned off.
    Dynamic Tone Mapping must be off: if the TV keeps it on, the user is
    asked to switch it off and the run waits (SystemExit after timeout)."""
    def read(keys) -> tuple[dict, dict]:
        result = lg.picture_settings({"keys": list(keys), "picture_mode": picture_mode, "signal_mode": "hdr10",
                                      "ignore_calibration_picture_mode": True})
        values = result.get("picture_settings") if isinstance(result.get("picture_settings"), dict) else {}
        return {k: str(values[k]) for k in keys if values.get(k) is not None}, result

    def steady(value: str, values: tuple) -> bool:
        return value.lower() in values

    before, result = read([key for key, _name, _steady in STEADY_PICTURE])
    log(f"HDR picture processing: {before} ({result.get('status')} {result.get('message') or ''})")
    if result.get("status") != "ok":
        say(f"Could not read the TV's picture settings ({result.get('message') or 'no answer'}). Check that "
            f"Dynamic Tone Mapping is Off ({DTM_MENU}).")
        return []
    turned_off = []
    for key, name, values in STEADY_PICTURE:
        value = before.get(key)
        if value is None or steady(value, values):
            continue                       # not on this model, or already steady
        written = lg.picture_settings_set({"settings": {key: "off"}, "readback_keys": [key],
                                           "picture_mode": picture_mode, "signal_mode": "hdr10",
                                           "keep_calibration_mode": False})
        now = read([key])[0].get(key, value)
        log(f"{key}: {value} -> {now} ({written.get('status')} {written.get('message') or ''})")
        if steady(now, values):
            turned_off.append(name)
            continue
        if key != "hdrDynamicToneMapping":
            say(f"{name} is {now} and the TV would not turn it off from the PC. If the check at the end "
                "says the picture is unsteady, turn it off on the TV.")
            continue
        say(f"Dynamic Tone Mapping is {now}, and the TV would not turn it off from the PC. It changes "
            "brightness scene by scene, so the picture would not match the calibration.")
        say(f"On the TV, open {DTM_MENU} and pick Off. The run continues by itself.")
        started = clock()
        while not steady(read([key])[0].get(key, now), values):
            if clock() - started > timeout:
                raise SystemExit(f"Dynamic Tone Mapping is still on. Set it to Off on the TV ({DTM_MENU}), "
                                 "then run LG HDR AutoCal again. Nothing was calibrated.")
            sleep(2)
        say("Dynamic Tone Mapping is off.")
    if turned_off:
        names = ", ".join(turned_off[:-1]) + " and " + turned_off[-1] if len(turned_off) > 1 else turned_off[0]
        say(f"Turned off {names} for this picture mode: "
            + ("they change brightness by themselves" if len(turned_off) > 1 else "it changes brightness by itself")
            + ", so the picture would not match the calibration.")
    return turned_off


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
# A steady TV gives the same grey twice (up and down the ladder), and light
# adds up: cyan = green + blue. A TV adjusting the picture by itself fails one
# or both (a G2 run that failed its check read cyan 37% short of green + blue).
REPEAT_TOLERANCE = 0.08
ADDITIVE_TOLERANCE = 0.12
STEADY_FROM_NITS = 1.0     # below this a Spyder5's own repeatability is too coarse
MIXES = (("Cyan", "Green", "Blue"), ("Magenta", "Red", "Blue"), ("Yellow", "Red", "Green"))


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
    BT.709 colours in the BT.2020 container, dE ITP (the metric made for HDR).
    The greys are read up the ladder and down again, and the mixed colours
    against their parts, so a TV changing the picture by itself shows up."""
    say("")
    say("Verifying the HDR result (independent measurement, TV out of calibration mode) ...")

    def measure(rgb01):
        show_signal(pattern, rgb01, area)
        time.sleep(settle)
        return read_with_recovery(meter, log)

    up = {signal: measure((signal / 100,) * 3) for signal in VERIFY_SIGNALS}
    rows = []
    for signal in VERIFY_SIGNALS[::-1]:
        down = measure((signal / 100,) * 3)
        mean = tuple((a + b) / 2 for a, b in zip(up[signal], down))
        row = grey_row(signal, mean, peak, floor)
        row["Y_readings"] = [up[signal][1], down[1]]
        row["unsteady"] = (mean[1] >= max(STEADY_FROM_NITS, 3 * floor)
                           and abs(up[signal][1] - down[1]) > REPEAT_TOLERANCE * mean[1])
        rows.insert(0, row)
    say(f"{'Signal':>6}  {'Y cd/m2':>9}  {'PQ target':>9}  {'x':>7}  {'y':>7}  {'dE ITP':>6}")
    for row in rows:
        mark = "  *" if row["below_floor"] else ("  (tone-mapped)" if row["tone_mapped"] else "")
        if row["unsteady"]:
            mark += "  (unsteady: {:.3f} then {:.3f})".format(*row["Y_readings"])
        say(f"{row['signal']:>5}%  {row['Y']:9.3f}  {row['target_Y']:9.3f}  {row['x']:7.4f}  {row['y']:7.4f}  "
            f"{row['de']:6.2f}{mark}")
    scored = [r["de"] for r in rows if not r["below_floor"] and not r["tone_mapped"]]
    colours = []
    for name, signal, target in colour_patches():
        xyz = measure(signal)
        total = sum(xyz) or 1.0
        colours.append({"name": name, "Y": xyz[1], "target_Y": target[1], "x": xyz[0] / total,
                        "y": xyz[1] / total, "target_x": target[0] / sum(target),
                        "target_y": target[1] / sum(target), "de": delta_e_itp(xyz, target)})
    by_name = {c["name"]: c for c in colours}
    for mix, first, second in MIXES:
        parts = by_name[first]["Y"] + by_name[second]["Y"]
        by_name[mix]["parts_Y"] = parts
        by_name[mix]["not_additive"] = parts > 0 and abs(by_name[mix]["Y"] / parts - 1) > ADDITIVE_TOLERANCE
    say(f"{'Colour':>9}  {'Y cd/m2':>9}  {'target':>9}  {'x':>7}  {'y':>7}  {'target x,y':>15}  {'dE ITP':>6}")
    for c in colours:
        mark = ""
        if c.get("not_additive"):
            first, second = next((a, b) for m, a, b in MIXES if m == c["name"])
            mark = f"  ({first.lower()} + {second.lower()} measured {c['parts_Y']:.2f})"
        say(f"{c['name']:>9}  {c['Y']:9.2f}  {c['target_Y']:9.2f}  {c['x']:7.4f}  {c['y']:7.4f}  "
            f"{c['target_x']:7.4f},{c['target_y']:7.4f}  {c['de']:6.2f}{mark}")
    summary = {"rows": rows, "colours": colours, "peak": peak,
               "average_de": sum(scored) / len(scored) if scored else math.nan,
               "max_de": max(scored) if scored else math.nan,
               "colour_average_de": sum(c["de"] for c in colours) / len(colours),
               "colour_max_de": max(c["de"] for c in colours),
               "unsteady": [r["signal"] for r in rows if r["unsteady"]],
               "not_additive": [c["name"] for c in colours if c.get("not_additive")]}
    summary["steady"] = not (summary["unsteady"] or summary["not_additive"])
    say(f"Greyscale: average dE ITP {summary['average_de']:.2f}, worst {summary['max_de']:.2f}   "
        f"Colours: average {summary['colour_average_de']:.2f}, worst {summary['colour_max_de']:.2f}")
    say("* below what the meter reads reliably; (tone-mapped) above half the peak, where LG's tone map "
        "rolls off. Both are shown, not scored.")
    if not summary["steady"]:
        say("")
        say("WARNING: these numbers are not the calibration. The TV changed the picture by itself during the check:")
        if summary["unsteady"]:
            say("  the same grey measured differently going up and coming down ("
                + ", ".join(f"{s:g}%" for s in summary["unsteady"]) + ").")
        if summary["not_additive"]:
            say("  mixed colours did not add up (" + ", ".join(summary["not_additive"]).lower()
                + "): each should measure its two colours added together.")
        say("Something on the TV is still adjusting brightness. For this picture mode, check that Dynamic Tone "
            "Mapping, AI Picture Pro, AI Brightness and Energy Saving are Off, and that the meter has not moved. "
            "Then run LG HDR AutoCal again.")
    return summary

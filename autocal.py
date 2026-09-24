#!/usr/bin/env python3
"""Panasonic GT60/VT60 grayscale white-balance AutoCal V4."""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path

from colour import code_fraction, metrics, signal_code, tint, tint_score, uv_error, xyz_to_uv
from domain import Measurement
from hardware import Meter, PatternHost, TVSession, Transcript, measure
from report import write_report
from solver import Model, best_integer_move, update_model, white_balance_improved


HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
# The TV has 10 grayscale points, so measure only where it can be
# corrected. Slot 100 acts on the 95% patch; 100% is set by two-point high.
MEASURE_LEVELS = (0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100)
DETAIL_SLOTS = tuple(range(10, 101, 10))
# Measured on this VT60 (session 20260924_185416): slot 100 moves the 95%
# patch 3.2x more than 100% for white balance, so it is corrected at 95%.
SLOT_PATCH = {100: 95}


def json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def save_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, default=json_default), encoding="utf-8")


def new_session(suffix: str) -> Path:
    sessions = HERE / "sessions"
    sessions.mkdir(exist_ok=True)
    path = sessions / (time.strftime("%Y%m%d_%H%M%S") + "_" + suffix)
    path.mkdir()
    return path


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))


def read_count(level: int, config: dict, fast: bool = False) -> int:
    if fast:
        return 1
    reads = config["meter"]["reads"]
    if level == 0:
        return int(reads["black"])
    if level <= 10:
        return int(reads["shadow"])
    if level == 100:
        return int(reads["white"])
    return int(reads["normal"])


def validate_meter_configuration(config: dict) -> None:
    meter = config["meter"]
    correction = Path(meter["correction_file"])
    if not correction.is_file():
        raise RuntimeError(f"Required Spyder5 plasma correction is missing: {correction}")
    header = correction.read_text(encoding="ascii", errors="ignore")[:2000]
    required = ('TECHNOLOGY "Plasma"', 'DISPLAY_TYPE_REFRESH "YES"')
    missing = [marker for marker in required if marker not in header]
    if missing:
        raise RuntimeError(
            f"Correction file is not the expected refresh-plasma CCSS: {correction}; "
            + ", ".join(missing)
        )
    args = list(meter["args"])
    if "-X" not in args or str(correction) not in args or "R:60" not in args:
        raise RuntimeError("Meter args must explicitly load the plasma CCSS and lock refresh to 60 Hz")


def validate_signal_path(rows: list[Measurement], config: dict) -> dict:
    """Check measured black/shadow/headroom symptoms in a verification sweep."""
    by_level = {row.level: row for row in rows}
    required = (0, 10, 95, 100)
    if any(level not in by_level for level in required):
        raise RuntimeError("Signal-path guard requires 0, 10, 95 and 100 percent measurements")
    black = by_level[0].xyz.Y
    white = by_level[100].xyz.Y
    span = white - black
    if span <= 0:
        raise RuntimeError("Signal-path guard found non-positive white-to-black luminance span")
    evidence = {
        "black_white_ratio": black / white,
        "ten_percent_above_black": (by_level[10].xyz.Y - black) / span,
        "headroom_above_95_percent": (white - by_level[95].xyz.Y) / span,
    }
    guard = config["signal_guard"]
    failures = []
    if evidence["black_white_ratio"] > float(guard["maximum_black_white_ratio"]):
        failures.append("black is raised")
    if evidence["ten_percent_above_black"] < float(guard["minimum_10_percent_above_black"]):
        failures.append("10% is crushed")
    if evidence["headroom_above_95_percent"] < float(guard["minimum_headroom_above_95_percent"]):
        failures.append("95% is clipped")
    if failures:
        raise RuntimeError(
            "Final signal-path check failed (" + ", ".join(failures) + "): "
            + json.dumps(evidence)
        )
    return evidence


TWO_POINT_HIGH = ("WB:HIR", "WB:HIB")
TWO_POINT_LOW = ("WB:LOR", "WB:LOB")
DETAIL_GAINS = ("WB:GNR", "WB:GNB")


class AutoCal:
    def __init__(self, tv: TVSession, pattern: PatternHost, meter: Meter,
                 log: Transcript, config: dict):
        self.tv = tv
        self.pattern = pattern
        self.meter = meter
        self.log = log
        self.config = config
        self.settle = float(config["pattern"]["settle_seconds"])
        self.signal_range = config["pattern"]["range"]
        self.curve = config["target"]["curve"]
        self.gamma = float(config["target"]["gamma"])
        self.solver_config = config["solver"]
        self.black_y = 0.0
        self.white_y: float | None = None
        # Learned u'v'-per-step response of each control pair, by stage key.
        self.models: dict[str, Model] = {}
        # Differences between readings at unchanged controls, by level.
        self.noise_samples: dict[int, list[float]] = {}

    def read(self, level: int, stage: str, fast: bool = False,
             settle: float | None = None) -> Measurement:
        expected = None
        if self.white_y:
            fraction = code_fraction(signal_code(level, self.signal_range), self.signal_range)
            expected = self.white_y * fraction ** self.gamma
        row = measure(self.pattern, self.meter, self.log, level,
                      read_count(level, self.config, fast),
                      self.settle if settle is None else settle, stage,
                      signal_range=self.signal_range, expected_y=expected)
        if row.read_count > 1 and row.uv_noise > 0:
            self._record_noise(level, row.uv_noise)
        return row

    def read_levels(self, levels: list[int] | tuple[int, ...], stage: str) -> list[Measurement]:
        return [self.read(level, stage) for level in sorted(set(levels))]

    def sweep(self, stage: str) -> list[Measurement]:
        return self.read_levels(MEASURE_LEVELS, stage)

    def _record_noise(self, level: int, value: float) -> None:
        self.noise_samples.setdefault(level, []).append(value)

    def noise(self, level: int) -> float:
        """Typical u'v' difference between readings at unchanged controls,
        interpolated between the nearest levels with evidence (meter noise
        rises steeply in the shadows, so a far darker level is no guide)."""
        if not self.noise_samples:
            return 0.0
        known = {lvl: statistics.median(values) for lvl, values in self.noise_samples.items()}
        if level in known:
            return known[level]
        below = [lvl for lvl in known if lvl < level]
        above = [lvl for lvl in known if lvl > level]
        if not below or not above:
            return known[max(below) if below else min(above)]
        low, high = max(below), min(above)
        share = (level - low) / (high - low)
        return known[low] + share * (known[high] - known[low])

    def _select(self, slot: int | None) -> None:
        if slot is not None:
            self.tv.select_point(slot)

    def _read_controls(self, codes: tuple[str, str], slot: int | None) -> dict[str, int]:
        self._select(slot)
        return {code: self.tv.get_number(code) for code in codes}

    def _write_controls(self, values: dict[str, int], slot: int | None) -> None:
        self._select(slot)
        for code, value in values.items():
            self.tv.set_number(code, value)

    def _response_column(self, code: str, current: int, primary: int,
                         slot: int | None, rolling: Measurement,
                         label: str) -> tuple[tuple[float, float], Measurement, dict]:
        step_size = int(self.solver_config["response_step_rgb"])
        trial = current + step_size if current <= 50 - step_size else current - step_size
        step = trial - current
        self._select(slot)
        self.tv.set_number(code, trial)
        changed = self.read(primary, f"{label}_{code}_trial", fast=True)
        self._select(slot)
        self.tv.set_number(code, current)
        restored = self.read(primary, f"{label}_{code}_restored", fast=True)
        # Same controls before and after the probe: their difference is the
        # meter noise plus drift at this level.
        self._record_noise(primary, math.hypot(restored.u - rolling.u, restored.v - rolling.v))
        reference_u = (rolling.u + restored.u) / 2.0
        reference_v = (rolling.v + restored.v) / 2.0
        response = ((changed.u - reference_u) / step,
                    (changed.v - reference_v) / step)
        evidence = {
            "code": code,
            "original": current,
            "trial": trial,
            "step": step,
            "response_uv_per_step": response,
            "rolling_before": rolling,
            "changed": changed,
            "restored": restored,
        }
        return response, restored, evidence

    def _probe(self, codes: tuple[str, str], current: dict[str, int], primary: int,
               slot: int | None, rolling: Measurement,
               label: str) -> tuple[Model, Measurement, list[dict]]:
        columns, evidence = [], []
        for code in codes:
            column, rolling, item = self._response_column(
                code, current[code], primary, slot, rolling, label)
            columns.append(column)
            evidence.append(item)
        return (columns[0], columns[1]), rolling, evidence

    def optimise_white_balance(self, label: str, primary: int, affected: list[int],
                               codes: tuple[str, str], slot: int | None,
                               cap: int, iterations: int, *, model_key: str,
                               inherit_from: str | None = None,
                               initial_primary: Measurement | None = None) -> dict:
        """Correct one red/blue pair at `primary`, guarding the `affected` levels.

        The move is chosen by simulating every whole-step setting against the
        learned response model. Probing happens only when no model exists, or
        when a move from an inherited or learned model fails. Readings at
        unchanged controls are always reused.
        """
        if initial_primary is not None and initial_primary.level != primary:
            raise ValueError("Initial measurement does not match the correction level")
        stop_uv = float(self.solver_config["white_balance_stop_uv"])
        floor_uv = float(self.solver_config["white_balance_minimum_improvement_uv"])
        weight = float(self.solver_config["model_update_weight"])
        model = self.models.get(model_key)
        source = "learned" if model else None
        if model is None and inherit_from in self.models:
            model, source = self.models[inherit_from], f"inherited:{inherit_from}"
        probed = False
        history = []
        latest = initial_primary
        start = None
        for iteration in range(1, iterations + 1):
            current = self._read_controls(codes, slot)
            before = latest or self.read(primary, f"{label}_before_{iteration}")
            if start is None:
                start = {"controls": current, "measurement": before}
            noise = self.noise(primary)
            if tint(before) <= max(stop_uv, noise):
                latest = before
                history.append({"iteration": iteration, "accepted": False,
                                "reason": "within_target", "controls": current,
                                "measurement": before, "tint": tint(before),
                                "noise": noise})
                break
            responses = []
            if model is None:
                model, before, responses = self._probe(codes, current, primary, slot, before, label)
                probed, source = True, "probe"
                noise = self.noise(primary)
            latest = before
            reference_rows = [
                before if level == primary else
                self.read(level, f"{label}_reference_{iteration}", fast=True)
                for level in sorted(set(affected))
            ]
            reference = tint_score(reference_rows)
            plan = best_integer_move(current, codes, uv_error(before), model, cap)
            if plan.move == (0, 0):
                if not probed:
                    # The borrowed model says no step helps; confirm by probing.
                    history.append({"iteration": iteration, "accepted": False,
                                    "reason": "no_move_reprobe", "controls": current,
                                    "model_source": source, "tint": tint(before)})
                    model = None
                    continue
                history.append({"iteration": iteration, "accepted": False,
                                "reason": "no_integer_move", "controls": current,
                                "model_source": source, "tint": tint(before)})
                break

            self._write_controls(dict(plan.values), slot)
            candidate_rows = self.read_levels(affected, f"{label}_candidate_{iteration}")
            candidate = next(row for row in candidate_rows if row.level == primary)
            candidate_score = tint_score(candidate_rows)
            model_before = model
            model = update_model(model, plan.move,
                                 (candidate.u - before.u, candidate.v - before.v), weight)
            threshold = max(floor_uv, noise)
            accepted = (white_balance_improved(reference.rms, candidate_score.rms, threshold)
                        and candidate_score.maximum <= reference.maximum + threshold)
            if not accepted:
                self._write_controls(current, slot)
            predicted = math.hypot(*plan.predicted_error)
            self.log.write(
                f"DECISION {label} iteration={iteration} {'ACCEPT' if accepted else 'RESTORE'} "
                f"model={source} move={plan.move} tint={reference.rms:.5f}->{candidate_score.rms:.5f} "
                f"predicted={predicted:.5f} measured={tint(candidate):.5f} "
                f"threshold={threshold:.5f}"
            )
            history.append({
                "iteration": iteration,
                "accepted": accepted,
                "model_source": source,
                "model": model_before,
                "controls_before": current,
                "controls_tried": dict(plan.values),
                "move": plan.move,
                "responses": responses,
                "before_error": uv_error(before),
                "predicted_error": plan.predicted_error,
                "measured_error": uv_error(candidate),
                "threshold": threshold,
                "reference_measurements": reference_rows,
                "reference_score": reference,
                "candidate_measurements": candidate_rows,
                "candidate_score": candidate_score,
            })
            if accepted:
                latest = candidate
                source = "learned" if source != "probe" else source
            elif probed:
                break
            else:
                # Controls are restored, so `before` is still valid. Measure
                # this point's own response before trying again.
                model = None
        if model is not None:
            self.models[model_key] = model
        return {
            "label": label,
            "primary": primary,
            "slot": slot,
            "codes": list(codes),
            "start": start,
            "end": {"controls": self._read_controls(codes, slot), "measurement": latest},
            "history": history,
        }

    def run(self) -> dict:
        result = {"version": 5, "workflow": "learn_then_simulate", "stages": {}}
        # Every tint and luminance target rests on this reading: settle longer
        # and use the full white read count.
        self.log.write("REFERENCE WHITE")
        white = self.read(100, "reference_white", settle=max(self.settle, 3.0))
        if white.xyz.Y <= 0:
            raise RuntimeError("Cannot calibrate: the white patch has no positive luminance")
        self.white_y = white.xyz.Y
        result["reference_white"] = white

        black = self.read(0, "preflight_black", fast=True)
        ratio = black.xyz.Y / white.xyz.Y
        result["preflight_black"] = {"measurement": black, "black_white_ratio": ratio}
        limit = float(self.config["signal_guard"]["maximum_black_white_ratio"])
        self.log.write(f"PREFLIGHT black={black.xyz.Y:.4f} white={white.xyz.Y:.2f} ratio={ratio:.5f}")
        if ratio > limit:
            raise RuntimeError(
                f"Black is raised ({black.xyz.Y:.3f} cd/m2, {ratio:.4f} of white; limit {limit}). "
                "Brightness is probably too high: set it with the AVS HD 709 black clipping "
                "pattern, then run again. No calibration values were changed."
            )
        self.black_y = black.xyz.Y

        stages = result["stages"]
        two_iterations = int(self.solver_config["two_point_iterations"])
        stages["two_point_high"] = self.optimise_white_balance(
            "two_point_high", 100, [60, 80, 100], TWO_POINT_HIGH, None,
            cap=4, iterations=two_iterations, model_key="two_point_high",
            initial_primary=white,
        )
        self.log.write("TWO-POINT LOW: correcting at 20%")
        stages["two_point_low"] = self.optimise_white_balance(
            "two_point_low", 20, [20, 30, 40], TWO_POINT_LOW, None,
            cap=3, iterations=two_iterations, model_key="two_point_low",
        )

        # Top down: probes at bright levels are quick and quiet, and each
        # lower point starts from the model its neighbour just refined.
        detail_iterations = int(self.solver_config["detail_iterations"])
        detail_results = []
        previous = None
        for slot in sorted(DETAIL_SLOTS, reverse=True):
            primary = SLOT_PATCH.get(slot, slot)
            key = f"detail_{slot}"
            detail_results.append(self.optimise_white_balance(
                key, primary, [primary], DETAIL_GAINS, slot,
                cap=4 if slot > 10 else 5, iterations=detail_iterations,
                model_key=key, inherit_from=previous,
            ))
            previous = key
        stages["detailed_white_balance"] = detail_results

        stages["final_high_polish"] = self.optimise_white_balance(
            "final_high", 100, [80, 90, 95, 100], TWO_POINT_HIGH, None,
            cap=2, iterations=1, model_key="two_point_high",
        )

        final = self.sweep("final_verification")
        result["final"] = final
        result["final_summary"] = summarise_sweep(final, self)
        result["signal_path"] = validate_signal_path(final, self.config)
        self.log.write("FINAL SIGNAL PATH PASS " + json.dumps(result["signal_path"]))
        return result


def summarise_sweep(rows: list[Measurement], autocal: AutoCal) -> dict:
    by_level = {row.level: row for row in rows}
    black_y, white_y = by_level[0].xyz.Y, by_level[100].xyz.Y
    points = []
    effective_gammas = []
    for row in sorted(rows, key=lambda item: item.level):
        item = metrics(row, black_y, white_y, autocal.curve, autocal.gamma)
        point = {"measurement": row, "metrics": item}
        points.append(point)
        if 0 < row.level < 100:
            relative = (row.xyz.Y - black_y) / max(white_y - black_y, 1e-12)
            if 0 < relative < 1:
                effective_gammas.append(math.log(relative) / math.log(row.signal_fraction))
    reportable = [point for point in points if point["measurement"].level > 0]
    worst_total = max(reportable, key=lambda point: point["metrics"].total_delta_e)
    worst_chroma = max(reportable, key=lambda point: point["metrics"].chroma_delta_e)
    tints = {point["measurement"].level: tint(point["measurement"]) for point in reportable}
    worst_tint_level = max(tints, key=tints.get)
    return {
        "curve": autocal.curve,
        "target_gamma": autocal.gamma,
        "black_y": black_y,
        "white_y": white_y,
        "average_delta_e_2000": statistics.mean(point["metrics"].total_delta_e for point in reportable),
        "maximum_delta_e_2000": worst_total["metrics"].total_delta_e,
        "worst_delta_e_level": worst_total["measurement"].level,
        "average_chroma_delta_e_2000": statistics.mean(point["metrics"].chroma_delta_e for point in reportable),
        "maximum_chroma_delta_e_2000": worst_chroma["metrics"].chroma_delta_e,
        "worst_chroma_level": worst_chroma["measurement"].level,
        "median_effective_gamma": statistics.median(effective_gammas),
        "average_tint_uv": statistics.mean(tints.values()),
        "maximum_tint_uv": tints[worst_tint_level],
        "worst_tint_level": worst_tint_level,
        "points": points,
    }


def print_summary(summary: dict) -> None:
    print(f"White point: average tint {summary['average_tint_uv']:.5f} u'v', worst "
          f"{summary['maximum_tint_uv']:.5f} at {summary['worst_tint_level']}%")
    print(f"Chroma dE2000 average {summary['average_chroma_delta_e_2000']:.2f}, maximum "
          f"{summary['maximum_chroma_delta_e_2000']:.2f} at {summary['worst_chroma_level']}%")
    print(f"dE2000 incl. luminance average {summary['average_delta_e_2000']:.2f}; "
          f"measured gamma {summary['median_effective_gamma']:.3f}")


def connect_tv(ip: str, log: Transcript, config: dict, retries: int = 3,
               cycles: int = 2, prompt=input, pause=time.sleep,
               factory=TVSession):
    current_ip = ip
    last_error = None
    for cycle in range(1, cycles + 1):
        for attempt in range(1, retries + 1):
            log.write(
                f"TV CONNECT {current_ip}:2048 attempt {attempt}/{retries} "
                f"cycle {cycle}/{cycles}"
            )
            try:
                return factory(
                    current_ip, log, config["tv"]["accepted_models"],
                    config["tv"]["mode"]
                )
            except OSError as exc:
                last_error = exc
                log.write(f"TV CONNECT RETRY {type(exc).__name__}: {exc}")
                if attempt < retries:
                    pause(1.0)
        if cycle < cycles:
            answer = prompt(
                f"TV did not accept a connection at {current_ip}:2048. "
                "Re-open isfccc Network until it says Waiting for Connection, "
                "then press Enter to retry; type a new IP to use it, or N to stop: "
            ).strip()
            if answer.upper() in {"N", "NO"}:
                raise RuntimeError("Operator stopped after TV connection failure")
            if answer:
                current_ip = answer
    raise RuntimeError(
        f"TV did not accept a connection at {current_ip}:2048 after "
        f"{retries * cycles} attempts; last error: {last_error!r}"
    )


def open_hardware(session: Path, log: Transcript, config: dict, ip: str,
                  meter_path: Path, prepare: bool = False):
    pattern = PatternHost(session, log, config)
    tv = meter = None
    snapshot = None
    try:
        pattern.start(100 if prepare else 50)
        tv = connect_tv(ip, log, config)
        answer = input("Patch showing on the Panasonic? Place the Spyder5 flat on its centre, "
                       "then press Enter (N to stop): ").strip().upper()
        if answer in {"N", "NO"}:
            raise RuntimeError("Operator rejected Panasonic pattern placement")
        # Initialise USB and load the correction. The first actual reading
        # belongs to two-point calibration, not a preflight.
        meter = Meter(meter_path, list(config["meter"]["args"]), log)
        snapshot = tv.snapshot()
        if prepare:
            save_json(session / "pre_calibration_snapshot.json", snapshot)
            if config["pattern"].get("linearize_video_lut", True):
                pattern.linearize_video_lut(meter_path.with_name("dispwin.exe"))
            log.write("START using existing TV picture and grayscale settings; snapshot saved")
        return pattern, tv, meter, snapshot
    except Exception:
        try:
            pattern.restore_video_lut()
        except Exception as lut_error:
            log.write("LUT RESTORE FAILED " + repr(lut_error))
        for resource in (meter, tv, pattern):
            if resource:
                try:
                    resource.close()
                except Exception:
                    pass
        raise


def close_hardware(pattern, tv, meter, log) -> None:
    for resource in (meter, tv, pattern, log):
        if resource:
            try:
                resource.close()
            except Exception:
                pass


def resolve_connection(config: dict, ip: str | None = None,
                       meter: str | None = None) -> tuple[str, Path]:
    """TV IP and spotread path from the command line, else config.json."""
    ip = ip or config["tv"]["default_ip"]
    meter_path = Path(meter or config["meter"]["default_executable"])
    if not meter_path.is_file():
        raise RuntimeError(f"spotread.exe not found: {meter_path} (set meter.default_executable "
                           "in config.json or pass --meter)")
    return ip, meter_path


PREPARE_TEXT = (
    "Before running: the TV has been on for 30-60 minutes, Brightness and Contrast are set "
    "with the AVS HD 709 clipping patterns, ISFccc Network shows Waiting for Connection, and "
    "Windows shows the Panasonic as the sole 1920x1080 secondary screen in Extend mode."
)


def run_autocal(ip_override: str | None = None, meter_override: str | None = None) -> int:
    config = load_config()
    session = new_session("autocal")
    log = Transcript(session / "autocal.log")
    pattern = tv = meter = None
    original = None
    writes_started = False
    try:
        validate_meter_configuration(config)
        print("\nPANASONIC GT60/VT60 WHITE-BALANCE AUTOCAL V5")
        print(PREPARE_TEXT)
        ip, meter_path = resolve_connection(config, ip_override, meter_override)
        pattern, tv, meter, original = open_hardware(
            session, log, config, ip, meter_path, prepare=True
        )
        save_json(session / "effective_config.json", config)
        writes_started = True
        result = AutoCal(tv, pattern, meter, log, config).run()
        result["pre_calibration_snapshot"] = original
        result["final_snapshot"] = tv.snapshot()
        save_json(session / "autocal_result.json", result)
        save_json(session / "final_snapshot.json", result["final_snapshot"])
        report = write_report(session / "report.html", result, config,
                              f"Panasonic {tv.model} white-balance AutoCal")
        print("\nAUTOCAL COMPLETE")
        print_summary(result["final_summary"])
        print("Report:", report)
        return 0
    except Exception as exc:
        log.write("AUTOCAL FAILED " + repr(exc))
        print("\nAUTOCAL FAILED:", exc)
        if writes_started and tv and original:
            try:
                tv.restore(original)
                pattern.restore_video_lut()
                log.write("ORIGINAL TV SNAPSHOT AND VIDEO LUT RESTORED")
                print("Original TV settings and video LUT were restored.")
            except Exception as restore_error:
                log.write("RESTORE FAILED " + repr(restore_error))
                print("Automatic restore failed. Snapshot:", session / "pre_calibration_snapshot.json")
        print("Evidence:", session)
        return 1
    finally:
        close_hardware(pattern, tv, meter, log)


def run_verification(ip_override: str | None = None, meter_override: str | None = None) -> int:
    config = load_config()
    session = new_session("verify")
    log = Transcript(session / "verification.log")
    pattern = tv = meter = None
    try:
        validate_meter_configuration(config)
        print("\nPANASONIC GT60/VT60 READ-ONLY VERIFICATION V5")
        print("Arm ISFccc Network at Waiting for Connection. No calibration values will be written.")
        ip, meter_path = resolve_connection(config, ip_override, meter_override)
        pattern, tv, meter, snapshot = open_hardware(session, log, config, ip, meter_path)
        save_json(session / "tv_snapshot.json", snapshot)
        save_json(session / "effective_config.json", config)
        autocal = AutoCal(tv, pattern, meter, log, config)
        rows = autocal.sweep("read_only_verification")
        summary = summarise_sweep(rows, autocal)
        verification = {"version": 5, "read_only": True, "measurements": rows, "summary": summary}
        save_json(session / "verification_result.json", verification)
        report = write_report(session / "report.html", verification, config,
                              f"Panasonic {tv.model} read-only verification")
        print("\nVERIFICATION COMPLETE - NO CALIBRATION VALUES WERE WRITTEN")
        print_summary(summary)
        print("Report:", report)
        return 0
    except Exception as exc:
        log.write("VERIFICATION FAILED " + repr(exc))
        print("\nVERIFICATION FAILED:", exc)
        print("Evidence:", session)
        return 1
    finally:
        close_hardware(pattern, tv, meter, log)


def measure_settle(pattern, meter, tv, log, delays=(0.25, 0.5, 1.0),
                   repeats: int = 2, sleep=time.sleep) -> dict:
    """Find the shortest wait after a patch or TV control change that still
    gives a settled reading. Compares against a fully settled 100% white."""
    def uv_y(xyz) -> tuple[float, float, float]:
        u, v = xyz_to_uv(xyz)
        return u, v, xyz.Y

    pattern.show(100)
    sleep(3.0)
    reference = [uv_y(meter.read()) for _ in range(3)]
    ref_u = statistics.median(row[0] for row in reference)
    ref_v = statistics.median(row[1] for row in reference)
    ref_y = statistics.median(row[2] for row in reference)
    noise_uv = max(math.hypot(u - ref_u, v - ref_v) for u, v, _ in reference)
    noise_y = max(abs(y - ref_y) / ref_y for _, _, y in reference)
    # Allow for plasma drift over the test; a late start to the Spyder
    # integration shows up as several percent of missing luminance.
    tolerance_uv = max(3 * noise_uv, 0.0005)
    tolerance_y = max(3 * noise_y, 0.01)

    def check(kind: str, delay: float, xyz) -> dict:
        u, v, y = uv_y(xyz)
        error_uv = math.hypot(u - ref_u, v - ref_v)
        error_y = abs(y - ref_y) / ref_y
        ok = error_uv <= tolerance_uv and error_y <= tolerance_y
        log.write(f"SETTLE {kind} delay={delay:.2f}s dY={error_y:.2%} duv={error_uv:.5f} "
                  f"{'OK' if ok else 'UNSETTLED'}")
        return {"kind": kind, "delay": delay, "error_y": error_y,
                "error_uv": error_uv, "settled": ok}

    trials = []
    for delay in delays:
        for _ in range(repeats):
            pattern.show(0)
            sleep(1.0)
            pattern.show(100)
            sleep(delay)
            trials.append(check("patch", delay, meter.read()))

    code = "WB:HIR"
    original = tv.get_number(code)
    changed = original + 6 if original <= 44 else original - 6
    try:
        for delay in delays:
            for _ in range(repeats):
                tv.set_number(code, changed)
                sleep(1.0)
                tv.set_number(code, original)
                sleep(delay)
                trials.append(check("control", delay, meter.read()))
    finally:
        tv.set_number(code, original)

    settled = [delay for delay in delays
               if all(trial["settled"] for trial in trials if trial["delay"] >= delay)]
    recommended = min(settled) if settled else 2.0
    return {"reference_noise_uv": noise_uv, "reference_noise_y": noise_y,
            "tolerance_uv": tolerance_uv, "tolerance_y": tolerance_y,
            "trials": trials, "recommended_settle_seconds": recommended}


def run_settle_test(ip_override: str | None = None, meter_override: str | None = None) -> int:
    config = load_config()
    session = new_session("settle")
    log = Transcript(session / "settle.log")
    pattern = tv = meter = None
    try:
        validate_meter_configuration(config)
        print("\nPANASONIC SETTLE-TIME TEST")
        print("Measures how soon a reading is stable after a patch or TV control change.")
        print("It toggles two-point high red by 6 steps and always puts it back.")
        print("Arm ISFccc Network at Waiting for Connection.")
        ip, meter_path = resolve_connection(config, ip_override, meter_override)
        pattern, tv, meter, snapshot = open_hardware(session, log, config, ip, meter_path)
        save_json(session / "tv_snapshot.json", snapshot)
        result = measure_settle(pattern, meter, tv, log)
        save_json(session / "settle_result.json", result)
        current = float(config["pattern"]["settle_seconds"])
        print(f"\nRecommended settle_seconds: {result['recommended_settle_seconds']} "
              f"(config.json currently {current})")
        print("Results:", session)
        return 0
    except Exception as exc:
        log.write("SETTLE TEST FAILED " + repr(exc))
        print("\nSETTLE TEST FAILED:", exc)
        print("Evidence:", session)
        return 1
    finally:
        close_hardware(pattern, tv, meter, log)


def main() -> int:
    parser = argparse.ArgumentParser(description="Panasonic GT60/VT60 white-balance AutoCal")
    parser.add_argument("mode", choices=("run", "verify", "settle"), nargs="?", default="run")
    parser.add_argument("--ip", help="TV IP address (default: tv.default_ip in config.json)")
    parser.add_argument("--meter", help="path to spotread.exe (default: meter.default_executable)")
    args = parser.parse_args()
    return {"run": run_autocal, "verify": run_verification,
            "settle": run_settle_test}[args.mode](args.ip, args.meter)


if __name__ == "__main__":
    sys.exit(main())

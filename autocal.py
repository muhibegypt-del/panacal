#!/usr/bin/env python3
"""Panasonic GT60/VT60 grayscale and gamma AutoCal V4."""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path

from colour import D65_UV, evaluate, metrics, target_y
from domain import Evaluation, Measurement
from hardware import Meter, PatternHost, TVSession, Transcript, measure
from solver import (UnusableResponse, gamma_improved, propose_gamma,
                    propose_red_blue, white_balance_improved)


HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
MEASURE_LEVELS = tuple(range(0, 101, 5))
DETAIL_SLOTS = tuple(range(10, 101, 10))


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
    required = (0, 5, 95, 100)
    if any(level not in by_level for level in required):
        raise RuntimeError("Signal-path guard requires 0, 5, 95 and 100 percent measurements")
    black = by_level[0].xyz.Y
    white = by_level[100].xyz.Y
    span = white - black
    if span <= 0:
        raise RuntimeError("Signal-path guard found non-positive white-to-black luminance span")
    evidence = {
        "black_white_ratio": black / white,
        "five_percent_above_black": (by_level[5].xyz.Y - black) / span,
        "headroom_above_95_percent": (white - by_level[95].xyz.Y) / span,
    }
    guard = config["signal_guard"]
    failures = []
    if evidence["black_white_ratio"] > float(guard["maximum_black_white_ratio"]):
        failures.append("black is raised")
    if evidence["five_percent_above_black"] < float(guard["minimum_5_percent_above_black"]):
        failures.append("5% is crushed")
    if evidence["headroom_above_95_percent"] < float(guard["minimum_headroom_above_95_percent"]):
        failures.append("95% is clipped")
    if failures:
        raise RuntimeError(
            "Final signal-path check failed (" + ", ".join(failures) + "): "
            + json.dumps(evidence)
        )
    return evidence


class AutoCal:
    def __init__(self, tv: TVSession, pattern: PatternHost, meter: Meter,
                 log: Transcript, config: dict):
        self.tv = tv
        self.pattern = pattern
        self.meter = meter
        self.log = log
        self.config = config
        self.settle = float(config["pattern"]["settle_seconds"])
        self.curve = config["target"]["curve"]
        self.gamma = float(config["target"]["gamma"])
        self.solver_config = config["solver"]
        self.black_y = 0.0
        self.white_y = 1.0

    def read(self, level: int, stage: str, fast: bool = False) -> Measurement:
        return measure(self.pattern, self.meter, self.log, level,
                       read_count(level, self.config, fast), self.settle, stage)

    def read_levels(self, levels: list[int] | tuple[int, ...], stage: str) -> list[Measurement]:
        return [self.read(level, stage) for level in sorted(set(levels))]

    def sweep(self, stage: str) -> list[Measurement]:
        return self.read_levels(MEASURE_LEVELS, stage)

    def score(self, rows: list[Measurement]) -> Evaluation:
        weights = {5: 0.35, 10: 0.70}
        return evaluate(rows, self.black_y, self.white_y, self.curve, self.gamma, weights)

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

    def optimise_white_balance(self, label: str, primary: int, affected: list[int],
                               codes: tuple[str, str], slot: int | None,
                               cap: int, iterations: int, *, direct: bool = False,
                               initial_primary: Measurement | None = None) -> list[dict]:
        history = []
        if initial_primary is not None and initial_primary.level != primary:
            raise ValueError("Initial measurement does not match the correction level")
        reusable_primary = initial_primary
        for iteration in range(1, iterations + 1):
            current = self._read_controls(codes, slot)
            if direct:
                # Start the response measurement at the point being corrected.
                # A just-accepted reading is still valid at unchanged controls.
                point = reusable_primary or self.read(primary, f"{label}_before_{iteration}", fast=True)
                initial_rows = [point]
                reusable_primary = None
            else:
                initial_rows = self.read_levels(affected, f"{label}_before_{iteration}")
            initial_score = self.score(initial_rows)
            if initial_score.chroma_score <= float(self.solver_config["white_balance_stop_delta_e"]):
                history.append({"iteration": iteration, "accepted": False,
                                "reason": "within_target", "controls": current,
                                "measurements": initial_rows, "score": initial_score})
                break

            initial_by_level = {row.level: row for row in initial_rows}
            rolling = initial_by_level[primary]
            columns = []
            responses = []
            for code in codes:
                column, rolling, evidence = self._response_column(
                    code, current[code], primary, slot, rolling, label
                )
                columns.append(column)
                responses.append(evidence)

            if direct:
                # Both response probes have been restored. Their last primary
                # reading is already our reference; only read the other guards.
                reference_rows = [
                    rolling if level == primary else
                    self.read(level, f"{label}_reference_{iteration}", fast=True)
                    for level in sorted(set(affected))
                ]
            else:
                reference_rows = self.read_levels(affected, f"{label}_reference_{iteration}")
            reference_score = self.score(reference_rows)
            reference = {row.level: row for row in reference_rows}[primary]
            try:
                proposal = propose_red_blue(
                    current=current,
                    codes=codes,
                    baseline=reference,
                    red_response_per_step=columns[0],
                    blue_response_per_step=columns[1],
                    target_uv=D65_UV,
                    cap=cap,
                    gain=float(self.solver_config["gain_rgb"]),
                )
            except UnusableResponse as exc:
                history.append({"iteration": iteration, "accepted": False,
                                "reason": str(exc), "controls": current,
                                "responses": responses, "score": reference_score})
                break
            if all(value == 0 for value in proposal.applied_move):
                history.append({"iteration": iteration, "accepted": False,
                                "reason": "no_integer_move", "controls": current,
                                "proposal": proposal, "score": reference_score})
                break

            self._write_controls(dict(proposal.values), slot)
            candidate_rows = self.read_levels(affected, f"{label}_candidate_{iteration}")
            candidate_score = self.score(candidate_rows)
            accepted = white_balance_improved(
                reference_score.chroma_score,
                candidate_score.chroma_score,
                float(self.solver_config["white_balance_minimum_improvement"]),
            ) and candidate_score.maximum_chroma_delta_e <= reference_score.maximum_chroma_delta_e + 0.15
            if not accepted:
                self._write_controls(current, slot)
            self.log.write(
                f"DECISION {label} iteration={iteration} {'ACCEPT' if accepted else 'RESTORE'} "
                f"chroma={reference_score.chroma_score:.3f}->{candidate_score.chroma_score:.3f} "
                f"max={reference_score.maximum_chroma_delta_e:.3f}->{candidate_score.maximum_chroma_delta_e:.3f}"
            )
            history.append({
                "iteration": iteration,
                "accepted": accepted,
                "controls_before": current,
                "proposal": proposal,
                "responses": responses,
                "reference_measurements": reference_rows,
                "reference_score": reference_score,
                "candidate_measurements": candidate_rows,
                "candidate_score": candidate_score,
            })
            if not accepted:
                break
            if direct:
                reusable_primary = next(row for row in candidate_rows if row.level == primary)
        return history

    def verify_slot_100_mapping(self, code: str, family: str) -> dict:
        """Prove whether the TV's nominal slot 100 controls the 95% patch."""
        slot = 100
        self.tv.select_point(slot)
        original = self.tv.get_number(code)
        if code == "PC:GGN":
            step_size = int(self.solver_config["response_step_gamma"])
        else:
            step_size = int(self.solver_config["response_step_rgb"])
        trial = original + step_size if original <= 50 - step_size else original - step_size
        step = trial - original
        before95 = self.read(95, f"{family}_slot100_map_95_before", fast=True)
        before100 = self.read(100, f"{family}_slot100_map_100_before", fast=True)
        self.tv.select_point(slot)
        self.tv.set_number(code, trial)
        changed95 = self.read(95, f"{family}_slot100_map_95_changed", fast=True)
        changed100 = self.read(100, f"{family}_slot100_map_100_changed", fast=True)
        self.tv.select_point(slot)
        self.tv.set_number(code, original)
        restored95 = self.read(95, f"{family}_slot100_map_95_restored", fast=True)
        restored100 = self.read(100, f"{family}_slot100_map_100_restored", fast=True)

        def effect(before: Measurement, changed: Measurement, restored: Measurement) -> float:
            if code == "PC:GGN":
                reference_y = math.sqrt(max(before.xyz.Y, 1e-12) * max(restored.xyz.Y, 1e-12))
                return abs(math.log(max(changed.xyz.Y, 1e-12) / reference_y) / step)
            reference_u = (before.u + restored.u) / 2.0
            reference_v = (before.v + restored.v) / 2.0
            return math.hypot(changed.u - reference_u, changed.v - reference_v) / abs(step)

        effect95 = effect(before95, changed95, restored95)
        effect100 = effect(before100, changed100, restored100)
        noise_floor = 0.0003 if code == "PC:GGN" else 0.00002
        confirmed = effect95 > max(noise_floor, effect100 * 1.15)
        result = {
            "family": family,
            "control": code,
            "confirmed_95_dominant": confirmed,
            "original": original,
            "trial": trial,
            "effect_per_step_95": effect95,
            "effect_per_step_100": effect100,
            "measurements": [before95, before100, changed95, changed100, restored95, restored100],
        }
        self.log.write(
            f"SLOT100 MAP family={family} code={code} "
            f"95effect={effect95:.6f} 100effect={effect100:.6f} confirmed={confirmed}"
        )
        return result

    def optimise_gamma(self, slot: int, primary: int, affected: list[int],
                       cap: int, iterations: int) -> list[dict]:
        history = []
        for iteration in range(1, iterations + 1):
            self.tv.select_point(slot)
            current = self.tv.get_number("PC:GGN")
            before_rows = self.read_levels(affected, f"gamma_{slot}_before_{iteration}")
            before_score = self.score(before_rows)
            primary_before = {row.level: row for row in before_rows}[primary]
            desired_y = target_y(primary, self.black_y, self.white_y, self.curve, self.gamma)
            primary_error = abs(math.log(max(primary_before.xyz.Y, 1e-12) / max(desired_y, 1e-12)))
            if primary_error <= float(self.solver_config["gamma_stop_log_error"]):
                history.append({"iteration": iteration, "accepted": False,
                                "reason": "within_target", "control": current,
                                "measurements": before_rows, "score": before_score})
                break

            step_size = int(self.solver_config["response_step_gamma"])
            trial = current + step_size if current <= 50 - step_size else current - step_size
            step = trial - current
            self.tv.select_point(slot)
            self.tv.set_number("PC:GGN", trial)
            changed = self.read(primary, f"gamma_{slot}_trial", fast=True)
            self.tv.select_point(slot)
            self.tv.set_number("PC:GGN", current)
            restored = self.read(primary, f"gamma_{slot}_restored", fast=True)
            reference_y = math.sqrt(max(primary_before.xyz.Y, 1e-12) * max(restored.xyz.Y, 1e-12))
            response = math.log(max(changed.xyz.Y, 1e-12) / reference_y) / step

            reference_rows = self.read_levels(affected, f"gamma_{slot}_reference_{iteration}")
            reference_score = self.score(reference_rows)
            reference = {row.level: row for row in reference_rows}[primary]
            try:
                candidate_value = propose_gamma(
                    current=current,
                    baseline=reference,
                    desired_y=desired_y,
                    log_y_response_per_step=response,
                    cap=cap,
                    gain=float(self.solver_config["gain_gamma"]),
                )
            except UnusableResponse as exc:
                history.append({"iteration": iteration, "accepted": False,
                                "reason": str(exc), "control": current,
                                "response": response, "score": reference_score})
                break
            if candidate_value == current:
                history.append({"iteration": iteration, "accepted": False,
                                "reason": "no_integer_move", "control": current,
                                "response": response, "score": reference_score})
                break

            self.tv.select_point(slot)
            self.tv.set_number("PC:GGN", candidate_value)
            candidate_rows = self.read_levels(affected, f"gamma_{slot}_candidate_{iteration}")
            candidate_score = self.score(candidate_rows)
            monotonic = all(left.xyz.Y < right.xyz.Y
                            for left, right in zip(candidate_rows, candidate_rows[1:]))
            accepted = monotonic and gamma_improved(
                reference_score.gamma_score,
                candidate_score.gamma_score,
                reference_score.chroma_score,
                candidate_score.chroma_score,
                float(self.solver_config["gamma_minimum_improvement"]),
                float(self.solver_config["allowed_chroma_regression"]),
            )
            if not accepted:
                self.tv.select_point(slot)
                self.tv.set_number("PC:GGN", current)
            self.log.write(
                f"DECISION gamma slot={slot} mapped={primary}% "
                f"{'ACCEPT' if accepted else 'RESTORE'} "
                f"error={reference_score.gamma_score:.4f}->{candidate_score.gamma_score:.4f} "
                f"chroma={reference_score.chroma_score:.3f}->{candidate_score.chroma_score:.3f}"
            )
            history.append({
                "iteration": iteration,
                "accepted": accepted,
                "control_before": current,
                "candidate_value": candidate_value,
                "response_log_y_per_step": response,
                "desired_y": desired_y,
                "reference_measurements": reference_rows,
                "reference_score": reference_score,
                "candidate_measurements": candidate_rows,
                "candidate_score": candidate_score,
                "monotonic": monotonic,
            })
            if not accepted:
                break
        return history

    def run(self) -> dict:
        result = {"version": 4, "workflow": "direct_two_point", "stages": {}}
        self.log.write("START TWO-POINT: measuring white, then correcting RGB; no opening sweep")
        white = self.read(100, "two_point_high_start", fast=True)
        if white.xyz.Y <= 0:
            raise RuntimeError("Cannot calibrate: the white patch has no positive luminance")
        self.white_y = white.xyz.Y
        # White-balance scoring compares D65 at each reading's own luminance.
        # Black is only needed later when establishing the gamma target.
        result["starting_white"] = white

        two_iterations = int(self.solver_config["two_point_iterations"])
        result["stages"]["two_point_high"] = self.optimise_white_balance(
            "two_point_high", 100, [60, 80, 100], ("WB:HIR", "WB:HIB"),
            None, cap=4, iterations=two_iterations, direct=True, initial_primary=white,
        )
        self.log.write("TWO-POINT LOW: measuring and correcting 10%")
        result["stages"]["two_point_low"] = self.optimise_white_balance(
            "two_point_low", 10, [10, 20, 30], ("WB:LOR", "WB:LOB"),
            None, cap=3, iterations=two_iterations, direct=True,
        )

        wb_slot100 = self.verify_slot_100_mapping("WB:GNR", "white_balance")
        result["stages"]["white_balance_slot100_mapping"] = wb_slot100
        detail_iterations = int(self.solver_config["detail_iterations"])
        detail_results = []
        for slot in DETAIL_SLOTS:
            if slot == 100 and not wb_slot100["confirmed_95_dominant"]:
                detail_results.append({"slot": slot, "skipped": "95_mapping_not_confirmed"})
                continue
            primary = 95 if slot == 100 else slot
            affected = sorted({max(5, primary - 5), primary, min(100, primary + 5)})
            detail_results.append({
                "slot": slot,
                "primary": primary,
                "history": self.optimise_white_balance(
                    f"detail_{slot}", primary, affected, ("WB:GNR", "WB:GNB"),
                    slot, cap=4 if slot > 10 else 5, iterations=detail_iterations,
                ),
            })
        result["stages"]["detailed_white_balance"] = detail_results

        result["stages"]["final_high_polish"] = self.optimise_white_balance(
            "final_high", 100, [80, 90, 95, 100], ("WB:HIR", "WB:HIB"),
            None, cap=2, iterations=1,
        )

        anchors = self.read_levels([0, 100], "post_white_balance_anchors")
        self.black_y = next(row.xyz.Y for row in anchors if row.level == 0)
        self.white_y = next(row.xyz.Y for row in anchors if row.level == 100)
        gamma_slot100 = self.verify_slot_100_mapping("PC:GGN", "gamma")
        result["stages"]["gamma_slot100_mapping"] = gamma_slot100
        gamma_iterations = int(self.solver_config["gamma_iterations"])
        gamma_results = []
        for slot in DETAIL_SLOTS:
            if slot == 100 and not gamma_slot100["confirmed_95_dominant"]:
                gamma_results.append({"slot": slot, "skipped": "95_mapping_not_confirmed"})
                continue
            primary = 95 if slot == 100 else slot
            affected = sorted({max(5, primary - 5), primary, min(100, primary + 5)})
            gamma_results.append({
                "slot": slot,
                "primary": primary,
                "history": self.optimise_gamma(
                    slot, primary, affected, cap=12, iterations=gamma_iterations
                ),
            })
        result["stages"]["gamma"] = gamma_results

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
                effective_gammas.append(math.log(relative) / math.log(row.level / 100.0))
    reportable = [point for point in points if point["measurement"].level > 0]
    worst_total = max(reportable, key=lambda point: point["metrics"].total_delta_e)
    worst_chroma = max(reportable, key=lambda point: point["metrics"].chroma_delta_e)
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
        "points": points,
    }


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
        answer = input("Press Enter if the patch is on the Panasonic; type N if it is not: ").strip().upper()
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


def run_autocal() -> int:
    config = load_config()
    session = new_session("autocal")
    log = Transcript(session / "autocal.log")
    pattern = tv = meter = None
    original = None
    writes_started = False
    try:
        validate_meter_configuration(config)
        print("\nPANASONIC GT60/VT60 AUTOCAL V4 - DIRECT TWO-POINT START")
        print("Windows must show the Panasonic as the sole 1920x1080 secondary screen in Extend mode.")
        print("V4 uses your existing TV settings and starts two-point correction after meter initialisation.")
        print("No opening grayscale sweep. Detailed calibration and final verification follow.")
        print("Arm ISFccc Network at Waiting for Connection.")
        input("Press Enter when the TV is armed; place the Spyder5 after the centre patch appears...")
        ip = input(f"TV IP [{config['tv']['default_ip']}]: ").strip() or config["tv"]["default_ip"]
        default_meter = config["meter"]["default_executable"]
        meter_text = input(f"spotread.exe [{default_meter}]: ").strip()
        meter_path = Path(meter_text or default_meter)
        if not meter_path.is_file():
            raise RuntimeError(f"spotread.exe not found: {meter_path}")
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
        summary = result["final_summary"]
        print("\nAUTOCAL COMPLETE")
        print(f"Average dE00 {summary['average_delta_e_2000']:.2f}; "
              f"maximum {summary['maximum_delta_e_2000']:.2f} at {summary['worst_delta_e_level']}%")
        print(f"Chroma-only average {summary['average_chroma_delta_e_2000']:.2f}; "
              f"gamma {summary['median_effective_gamma']:.3f}")
        print("Results:", session)
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


def run_verification() -> int:
    config = load_config()
    session = new_session("verify")
    log = Transcript(session / "verification.log")
    pattern = tv = meter = None
    try:
        validate_meter_configuration(config)
        print("\nPANASONIC GT60/VT60 READ-ONLY VERIFICATION V4")
        print("Arm ISFccc Network at Waiting for Connection. No calibration values will be written.")
        input("Press Enter when the TV is armed; place the Spyder5 after the centre patch appears...")
        ip = input(f"TV IP [{config['tv']['default_ip']}]: ").strip() or config["tv"]["default_ip"]
        default_meter = config["meter"]["default_executable"]
        meter_text = input(f"spotread.exe [{default_meter}]: ").strip()
        meter_path = Path(meter_text or default_meter)
        if not meter_path.is_file():
            raise RuntimeError(f"spotread.exe not found: {meter_path}")
        pattern, tv, meter, snapshot = open_hardware(session, log, config, ip, meter_path)
        save_json(session / "tv_snapshot.json", snapshot)
        save_json(session / "effective_config.json", config)
        autocal = AutoCal(tv, pattern, meter, log, config)
        rows = autocal.sweep("read_only_verification")
        summary = summarise_sweep(rows, autocal)
        save_json(session / "verification_result.json", {
            "version": 4, "read_only": True, "measurements": rows, "summary": summary
        })
        print("\nVERIFICATION COMPLETE - NO CALIBRATION VALUES WERE WRITTEN")
        print(f"Average dE00 {summary['average_delta_e_2000']:.2f}; "
              f"maximum {summary['maximum_delta_e_2000']:.2f} at {summary['worst_delta_e_level']}%")
        print(f"Chroma-only average {summary['average_chroma_delta_e_2000']:.2f}; "
              f"gamma {summary['median_effective_gamma']:.3f}")
        print("Results:", session)
        return 0
    except Exception as exc:
        log.write("VERIFICATION FAILED " + repr(exc))
        print("\nVERIFICATION FAILED:", exc)
        print("Evidence:", session)
        return 1
    finally:
        close_hardware(pattern, tv, meter, log)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("run", "verify"), nargs="?", default="run")
    args = parser.parse_args()
    return run_autocal() if args.mode == "run" else run_verification()


if __name__ == "__main__":
    sys.exit(main())

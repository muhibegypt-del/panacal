"""Pure colour science and target calculations for Panasonic AutoCal."""
from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping

from domain import ErrorMetrics, Evaluation, Measurement, XYZ


D65_XY = (0.3127, 0.3290)
D65_XYZ = XYZ(0.9504559271, 1.0, 1.0890577508)


def xyz_to_xyy(xyz: XYZ) -> tuple[float, float, float]:
    total = xyz.X + xyz.Y + xyz.Z
    if total <= 0:
        raise ValueError(f"XYZ has no positive tristimulus sum: {xyz}")
    return xyz.X / total, xyz.Y / total, xyz.Y


def xy_to_uv(x: float, y: float) -> tuple[float, float]:
    denominator = -2.0 * x + 12.0 * y + 3.0
    if abs(denominator) < 1e-15:
        raise ValueError("xy cannot be converted to CIE 1976 u'v'")
    return 4.0 * x / denominator, 9.0 * y / denominator


def xyz_to_uv(xyz: XYZ) -> tuple[float, float]:
    x, y, _ = xyz_to_xyy(xyz)
    return xy_to_uv(x, y)


D65_UV = xy_to_uv(*D65_XY)


def xyz_to_lab(xyz: XYZ, white: XYZ = D65_XYZ) -> tuple[float, float, float]:
    delta = 6.0 / 29.0

    def f(value: float) -> float:
        if value > delta**3:
            return value ** (1.0 / 3.0)
        return value / (3.0 * delta**2) + 4.0 / 29.0

    fx = f(xyz.X / white.X)
    fy = f(xyz.Y / white.Y)
    fz = f(xyz.Z / white.Z)
    return 116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)


def delta_e_2000(lab1: tuple[float, float, float],
                 lab2: tuple[float, float, float]) -> float:
    """CIEDE2000 using the reference Sharma et al. formulation."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    C1, C2 = math.hypot(a1, b1), math.hypot(a2, b2)
    Cbar = (C1 + C2) / 2.0
    G = 0.5 * (1.0 - math.sqrt(Cbar**7 / (Cbar**7 + 25.0**7)))
    ap1, ap2 = (1.0 + G) * a1, (1.0 + G) * a2
    Cp1, Cp2 = math.hypot(ap1, b1), math.hypot(ap2, b2)
    hp1 = math.degrees(math.atan2(b1, ap1)) % 360.0 if Cp1 else 0.0
    hp2 = math.degrees(math.atan2(b2, ap2)) % 360.0 if Cp2 else 0.0
    dL = L2 - L1
    dC = Cp2 - Cp1
    dh = hp2 - hp1
    if Cp1 * Cp2 == 0:
        dh = 0.0
    elif dh > 180.0:
        dh -= 360.0
    elif dh < -180.0:
        dh += 360.0
    dH = 2.0 * math.sqrt(Cp1 * Cp2) * math.sin(math.radians(dh / 2.0))
    Lbar = (L1 + L2) / 2.0
    Cpbar = (Cp1 + Cp2) / 2.0
    if Cp1 * Cp2 == 0:
        hpbar = hp1 + hp2
    elif abs(hp1 - hp2) <= 180.0:
        hpbar = (hp1 + hp2) / 2.0
    elif hp1 + hp2 < 360.0:
        hpbar = (hp1 + hp2 + 360.0) / 2.0
    else:
        hpbar = (hp1 + hp2 - 360.0) / 2.0
    T = (1.0 - 0.17 * math.cos(math.radians(hpbar - 30.0))
         + 0.24 * math.cos(math.radians(2.0 * hpbar))
         + 0.32 * math.cos(math.radians(3.0 * hpbar + 6.0))
         - 0.20 * math.cos(math.radians(4.0 * hpbar - 63.0)))
    SL = 1.0 + 0.015 * (Lbar - 50.0) ** 2 / math.sqrt(20.0 + (Lbar - 50.0) ** 2)
    SC = 1.0 + 0.045 * Cpbar
    SH = 1.0 + 0.015 * Cpbar * T
    dtheta = 30.0 * math.exp(-((hpbar - 275.0) / 25.0) ** 2)
    RC = 2.0 * math.sqrt(Cpbar**7 / (Cpbar**7 + 25.0**7))
    RT = -math.sin(math.radians(2.0 * dtheta)) * RC
    l, c, h = dL / SL, dC / SC, dH / SH
    return math.sqrt(max(0.0, l*l + c*c + h*h + RT*c*h))


def power_target_y(level: int, black_y: float, white_y: float,
                   gamma: float = 2.4) -> float:
    p = min(1.0, max(0.0, level / 100.0))
    return black_y + (white_y - black_y) * p**gamma


def bt1886_target_y(level: int, black_y: float, white_y: float,
                    gamma: float = 2.4) -> float:
    """ITU-R BT.1886 EOTF anchored to the measured black and white."""
    if black_y < 0 or white_y <= black_y:
        raise ValueError("BT.1886 requires 0 <= black < white")
    p = min(1.0, max(0.0, level / 100.0))
    black_root = black_y ** (1.0 / gamma)
    white_root = white_y ** (1.0 / gamma)
    return ((white_root - black_root) * p + black_root) ** gamma


def target_y(level: int, black_y: float, white_y: float,
             curve: str = "power", gamma: float = 2.4) -> float:
    if curve == "power":
        return power_target_y(level, black_y, white_y, gamma)
    if curve == "bt1886":
        return bt1886_target_y(level, black_y, white_y, gamma)
    raise ValueError(f"Unknown target curve: {curve}")


def chroma_delta_e(measurement: Measurement, white_y: float) -> float:
    """CIEDE2000 to D65 at the measurement's own luminance."""
    if white_y <= 0:
        raise ValueError("white_y must be positive")
    scale = measurement.xyz.Y / white_y
    measured = XYZ(measurement.xyz.X / white_y,
                   measurement.xyz.Y / white_y,
                   measurement.xyz.Z / white_y)
    target = XYZ(D65_XYZ.X * scale, scale, D65_XYZ.Z * scale)
    return delta_e_2000(xyz_to_lab(measured), xyz_to_lab(target))


def metrics(measurement: Measurement, black_y: float, white_y: float,
            curve: str = "power", gamma: float = 2.4) -> ErrorMetrics:
    desired_y = target_y(measurement.level, black_y, white_y, curve, gamma)
    log_error = 0.0
    if measurement.level > 0:
        log_error = math.log(max(measurement.xyz.Y, 1e-12) / max(desired_y, 1e-12))
    measured = XYZ(measurement.xyz.X / white_y, measurement.xyz.Y / white_y, measurement.xyz.Z / white_y)
    target_scale = desired_y / white_y
    target = XYZ(D65_XYZ.X * target_scale, target_scale, D65_XYZ.Z * target_scale)
    return ErrorMetrics(
        total_delta_e=delta_e_2000(xyz_to_lab(measured), xyz_to_lab(target)),
        chroma_delta_e=chroma_delta_e(measurement, white_y),
        target_y=desired_y,
        log_y_error=log_error,
    )


def evaluate(measurements: Iterable[Measurement], black_y: float, white_y: float,
             curve: str = "power", gamma: float = 2.4,
             weights: Mapping[int, float] | None = None) -> Evaluation:
    rows = list(measurements)
    if not rows:
        raise ValueError("At least one measurement is required")
    values = [(row, metrics(row, black_y, white_y, curve, gamma)) for row in rows]
    effective_weights = [max(0.0, (weights or {}).get(row.level, 1.0)) for row, _ in values]
    total_weight = sum(effective_weights)
    if total_weight <= 0:
        raise ValueError("Evaluation weights must contain a positive value")
    chroma_mse = sum(w * item.chroma_delta_e**2
                     for w, (_, item) in zip(effective_weights, values)) / total_weight
    gamma_mse = sum(w * item.log_y_error**2
                    for w, (_, item) in zip(effective_weights, values)
                    if item.target_y > black_y) / total_weight
    return Evaluation(
        chroma_score=math.sqrt(chroma_mse),
        gamma_score=math.sqrt(gamma_mse),
        maximum_chroma_delta_e=max(item.chroma_delta_e for _, item in values),
    )


def median_xyz(values: Iterable[XYZ]) -> XYZ:
    rows = list(values)
    if not rows:
        raise ValueError("At least one XYZ reading is required")
    return XYZ(*(statistics.median(channel) for channel in zip(*(row.as_tuple() for row in rows))))


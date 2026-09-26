"""Is this log good enough to tune from?  If not: what to change and how to fly.

``assess()`` measures how well a log covers what the analysis needs
(flight time, sample rate, logged fields, throttle range, stick inputs per
axis, throttle chops, flight mode) and returns issues.  Each issue carries
the FC settings to change and/or the flight-plan segments that would fix it,
so an LLM (or a human) can tell the pilot exactly what to do next.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

from ..blackbox.parser import EV_LOG_END, FlightLog
from .flight_data import FlightData, prepare, segments

PURPOSES = ("tuning", "race")

# rcModeActivationMask bits logged in the slow frame (stable across 4.x)
BOX_ANGLE = 1 << 1
BOX_HORIZON = 1 << 2


@dataclass
class Issue:
    id: str
    severity: str                   # blocker / warning / info
    message: str
    settings: dict[str, str] = field(default_factory=dict)   # recommended CLI values
    segments: list[str] = field(default_factory=list)        # flight-plan segment ids that fix it


@dataclass
class DataQuality:
    purpose: str
    sufficient: bool
    score: int
    metrics: dict
    issues: list[Issue]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["missing_segments"] = self.missing_segments()
        d["recommended_settings"] = self.recommended_settings()
        return d

    def missing_segments(self) -> list[str]:
        out: list[str] = []
        for i in self.issues:
            for s in i.segments:
                if s not in out:
                    out.append(s)
        return out

    def recommended_settings(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for i in self.issues:
            out.update(i.settings)
        return out


# ---------------------------------------------------------------------------
def count_snaps(sp: np.ndarray, fs: float, mask: np.ndarray, threshold: float) -> int:
    """Number of quick stick deflections: |setpoint| from < threshold/3 to > threshold within 150 ms."""
    a = np.abs(sp)
    lag = max(1, int(0.15 * fs))
    if len(a) <= lag:
        return 0
    rising = (a[lag:] > threshold) & (a[:-lag] < threshold / 3) & mask[lag:]
    events = segments(rising, 1)
    # merge events closer than 0.3 s
    count, last = 0, -10 ** 9
    for s, _e in events:
        if s - last > 0.3 * fs:
            count += 1
        last = s
    return count


def count_throttle_events(thr: np.ndarray, fs: float, mask: np.ndarray) -> tuple[int, int]:
    """(chops, punches): throttle falls > 25 % / rises > 40 % to > 70 % within 150 / 300 ms."""
    def events(cond):
        n, last = 0, -10 ** 9
        for s, _e in segments(cond, 1):
            if s - last > 0.5 * fs:
                n += 1
            last = s
        return n

    l1, l2 = int(0.15 * fs), int(0.3 * fs)
    chop = np.zeros(len(thr), bool)
    punch = np.zeros(len(thr), bool)
    if len(thr) > l2:
        chop[l1:] = (thr[:-l1] - thr[l1:] > 0.25) & mask[l1:]
        punch[l2:] = (thr[l2:] - thr[:-l2] > 0.40) & (thr[l2:] > 0.7) & mask[l2:]
    return events(chop), events(punch)


def _angle_mode_fraction(log: FlightLog) -> float:
    if log.slow_frames is None or "flightModeFlags" not in log.slow_names:
        return 0.0
    col = log.slow_names.index("flightModeFlags") + 1  # column 0 is time
    flags = log.slow_frames[:, col]
    return float(np.mean((flags & (BOX_ANGLE | BOX_HORIZON)) != 0)) if len(flags) else 0.0


def pid_loop_hz_from_headers(log: FlightLog) -> float | None:
    looptime = log.header_int("looptime")
    denom = log.header_int("pid_process_denom", 1) or 1
    if looptime:
        return 1e6 / looptime / denom
    return None


def bytes_per_second(log: FlightLog, size_bytes: int | None) -> float | None:
    if size_bytes and log.duration_s > 1:
        return size_bytes / log.duration_s
    return None


# ---------------------------------------------------------------------------
def assess(log: FlightLog, fd: FlightData | None = None, purpose: str = "tuning",
           log_size_bytes: int | None = None, flash_total_bytes: int | None = None,
           planned_duration_s: float | None = None) -> DataQuality:
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {PURPOSES}")
    issues: list[Issue] = []
    metrics: dict = {}
    try:
        fd = fd or prepare(log)
    except ValueError as e:
        issues.append(Issue("unusable_log", "blocker", f"ログを解析できません: {e}", segments=["hover"]))
        return DataQuality(purpose, False, 0, metrics, issues)

    fs = fd.fs
    air = fd.airborne
    air_s = fd.flight_time_s
    version = log.firmware_version
    modern = version >= (4, 3, 0) or version[0] >= 2025
    metrics.update(airborne_s=round(air_s, 1), duration_s=round(log.duration_s, 1),
                   sample_rate_hz=round(fs), firmware=log.firmware_revision)

    # --- flight time ----------------------------------------------------------
    need_s = 30 if purpose == "tuning" else 60
    if air_s < 10:
        issues.append(Issue("flight_too_short", "blocker",
                            f"飛行時間が {air_s:.0f} 秒しかありません。解析には最低 {need_s} 秒の飛行が必要です。",
                            segments=["hover", "throttle_sweep", "roll_snaps", "pitch_snaps"]))
    elif air_s < need_s:
        issues.append(Issue("flight_short", "warning",
                            f"飛行時間 {air_s:.0f} 秒は短めです ({need_s} 秒以上を推奨)。"))

    # --- sample rate ------------------------------------------------------------
    pid_hz = pid_loop_hz_from_headers(log)
    metrics["pid_loop_hz"] = round(pid_hz) if pid_hz else None
    min_rate = 1000 if purpose == "tuning" else 500
    if fs < min_rate:
        rec = recommend_sample_rate(pid_hz, purpose, bytes_per_frame(log, log_size_bytes),
                                    flash_total_bytes, planned_duration_s)
        issues.append(Issue("low_sample_rate", "blocker" if fs < 500 else "warning",
                            f"Blackbox 記録レート {fs:.0f}Hz では {fs / 2:.0f}Hz 以上のノイズが見えません"
                            f" (モーターノイズは 200〜800Hz)。",
                            settings={"blackbox_sample_rate": rec} if rec else {}))

    # --- logged fields ----------------------------------------------------------
    def need_field(name, setting, sev, msg):
        if name not in log.field_names:
            issues.append(Issue(f"missing_{setting}", sev, msg,
                                settings={f"blackbox_disable_{setting}": "OFF"} if modern else {}))

    if fd.gyro_raw is None:
        if modern:
            issues.append(Issue("missing_gyrounfilt", "warning",
                                "フィルター前 Gyro (gyroUnfilt) が記録されていないため、フィルターの効果が評価できません。",
                                settings={"blackbox_disable_gyrounfilt": "OFF"}))
        else:
            issues.append(Issue("missing_gyrounfilt", "warning",
                                "フィルター前 Gyro が記録されていません (BF4.2 以前は debug_mode=GYRO_SCALED が必要)。",
                                settings={"debug_mode": "GYRO_SCALED"}))
    need_field("setpoint[0]", "setpoint", "blocker", "setpoint が記録されていないためステップ応答を計算できません。")
    need_field("axisP[0]", "pids", "warning", "PID 項が記録されていないため D-term ノイズを評価できません。")
    need_field("motor[0]", "motors", "warning", "モーター出力が記録されていないため飽和・離陸判定の精度が落ちます。")
    dshot_bidir = str(log.headers.get("dshot_bidir", "0")) in ("1", "ON")
    if dshot_bidir and "eRPM[0]" not in log.field_names:
        issues.append(Issue("missing_rpm", "warning", "双方向 DShot 有効ですが eRPM が記録されていません。",
                            settings={"blackbox_disable_rpm": "OFF"} if modern else {}))
    dbg = log.header_int("debug_mode", 0) or 0
    if modern and dbg not in (0, 6) and purpose == "race":
        issues.append(Issue("debug_mode_on", "info", "debug_mode が有効で Flash 容量を消費しています。",
                            settings={"debug_mode": "NONE"}))

    # --- log integrity / flash full ----------------------------------------------
    total = sum(log.stats.get(k, 0) for k in ("I", "P")) + log.stats.get("corrupt", 0)
    corrupt_ratio = log.stats.get("corrupt", 0) / total if total else 0.0
    metrics["corrupt_ratio"] = round(corrupt_ratio, 4)
    ended = any(e.type == EV_LOG_END for e in log.events)
    metrics["log_end_marker"] = ended
    if corrupt_ratio > 0.01:
        issues.append(Issue("corrupt_frames", "warning",
                            f"破損フレームが {corrupt_ratio * 100:.1f}% あります (Flash/SD の不調、または容量不足)。"))
    if not ended:
        issues.append(Issue("log_not_closed", "info",
                            "ログが正常終了していません。Flash が満杯になったか、ディスアーム前に電源が切れた可能性があります。"
                            " 飛行前に Blackbox を消去し、必要なら記録レートを下げてください。"))

    # --- throttle coverage -----------------------------------------------------------
    thr = fd.throttle[air] if air.any() else fd.throttle
    bins = np.histogram(thr, bins=[0, 0.2, 0.4, 0.6, 0.8, 1.01])[0] / fs
    metrics["throttle_time_s"] = {k: round(float(v), 1) for k, v in
                                  zip(("0-20", "20-40", "40-60", "60-80", "80-100"), bins)}
    metrics["throttle_p95"] = round(float(np.percentile(thr, 95)), 2) if len(thr) else 0.0
    chops, punches = count_throttle_events(fd.throttle, fs, air)
    metrics["throttle_chops"] = chops
    metrics["punch_outs"] = punches
    if purpose == "tuning":
        if (bins[3] + bins[4]) < 2.0 or punches < 2:
            issues.append(Issue("no_high_throttle", "warning",
                                "高スロットル域 (60% 以上) のデータが不足しています。高スロットル時のノイズ・発振が評価できません。",
                                segments=["punch_outs", "throttle_sweep"]))
        if (bins > 1.0).sum() < 4:
            issues.append(Issue("narrow_throttle_range", "warning",
                                "スロットル域の偏りが大きく、ノイズとスロットルの関係 (モーターノイズ/フレーム共振の判別) が不確かです。",
                                segments=["throttle_sweep"]))
        if chops < 3:
            issues.append(Issue("no_throttle_chops", "warning",
                                f"スロットルを急に絞る動作が {chops} 回しかなく、プロップウォッシュを評価できません (3 回以上必要)。",
                                segments=["propwash_chops"]))

    # --- stick excitation per axis ------------------------------------------------------
    snaps = {}
    if fd.setpoint is not None:
        for k, (axis, th) in enumerate((("roll", 300.0), ("pitch", 300.0), ("yaw", 150.0))):
            snaps[axis] = count_snaps(fd.setpoint[:, k], fs, air, th)
        metrics["stick_snaps"] = snaps
        want = {"roll": 10, "pitch": 10, "yaw": 6} if purpose == "tuning" else {"roll": 5, "pitch": 5, "yaw": 3}
        for axis, n in snaps.items():
            if n < want[axis]:
                issues.append(Issue(f"few_{axis}_inputs", "warning" if n else "blocker",
                                    f"{axis} 軸の素早いスティック入力が {n} 回しかありません ({want[axis]} 回以上必要)。"
                                    " ステップ応答の信頼度が低くなります。",
                                    segments=[f"{axis}_snaps"]))

    # --- flight mode --------------------------------------------------------------------
    angle_frac = _angle_mode_fraction(log)
    metrics["angle_horizon_fraction"] = round(angle_frac, 2)
    if angle_frac > 0.2:
        issues.append(Issue("self_level_mode", "blocker" if angle_frac > 0.7 else "warning",
                            f"飛行時間の {angle_frac * 100:.0f}% が Angle/Horizon モードです。レートモード (Acro) の"
                            " PID を評価するため、Acro モード (Air Mode 有効) で飛行してください。",
                            segments=["mode_acro"]))

    # --- race: flash budget -----------------------------------------------------------
    bps = bytes_per_second(log, log_size_bytes)
    if bps:
        metrics["log_bytes_per_s"] = round(bps)
        if flash_total_bytes:
            metrics["flash_minutes_at_this_rate"] = round(flash_total_bytes / bps / 60, 1)
            if planned_duration_s and flash_total_bytes / bps < planned_duration_s * 1.1:
                rec = recommend_sample_rate(pid_hz, purpose, bytes_per_frame(log, log_size_bytes),
                                            flash_total_bytes, planned_duration_s)
                issues.append(Issue("flash_too_small", "warning",
                                    f"この記録レートでは Flash に {flash_total_bytes / bps:.0f} 秒しか記録できません"
                                    f" (予定 {planned_duration_s:.0f} 秒)。",
                                    settings={"blackbox_sample_rate": rec} if rec else {}))

    sev_w = {"blocker": 35, "warning": 12, "info": 3}
    score = max(0, 100 - sum(sev_w[i.severity] for i in issues))
    sufficient = not any(i.severity == "blocker" for i in issues) and score >= 50
    return DataQuality(purpose, sufficient, score, metrics, issues)


# ---------------------------------------------------------------------------
SAMPLE_RATE_DENOMS = {"1/1": 1, "1/2": 2, "1/4": 4, "1/8": 8, "1/16": 16}
DEFAULT_BYTES_PER_FRAME = 50.0


def bytes_per_frame(log: FlightLog | None, size_bytes: int | None) -> float:
    if log is not None and size_bytes and len(log.frames):
        return size_bytes / len(log.frames)
    return DEFAULT_BYTES_PER_FRAME


def recommend_sample_rate(pid_hz: float | None, purpose: str, bpf: float = DEFAULT_BYTES_PER_FRAME,
                          flash_bytes: int | None = None, duration_s: float | None = None) -> str | None:
    """Pick ``blackbox_sample_rate``: ~2 kHz for tuning (>= 1.6 kHz), as high as the flash
    allows for races (>= 500 Hz), always fitting ``duration_s`` into ``flash_bytes``."""
    if not pid_hz:
        return None
    candidates = []
    for name, d in SAMPLE_RATE_DENOMS.items():
        rate = pid_hz / d
        fits = True
        if flash_bytes and duration_s:
            fits = rate * bpf * duration_s * 1.1 <= flash_bytes
        candidates.append((name, rate, fits))
    if purpose == "tuning":
        good = [c for c in candidates if c[2] and 1600 <= c[1] <= 4100]
        if good:
            return min(good, key=lambda c: abs(c[1] - 2000))[0]
    fitting = [c for c in candidates if c[2] and c[1] >= 500]
    if fitting:
        return max(fitting, key=lambda c: c[1] if purpose == "race" else -abs(c[1] - 2000))[0]
    fitting = [c for c in candidates if c[2]]
    return max(fitting, key=lambda c: c[1])[0] if fitting else "1/16"

"""Convert a decoded :class:`FlightLog` into physical-unit arrays for analysis."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..blackbox.parser import FlightLog

AXIS_NAMES = ("roll", "pitch", "yaw")

# debug_mode index of GYRO_SCALED in Betaflight 4.x (debug[0..2] = unfiltered gyro)
DEBUG_GYRO_SCALED = 6


@dataclass
class FlightData:
    log: FlightLog
    fs: float                     # sample rate of the logged data (Hz)
    t: np.ndarray                 # seconds, uniform grid
    gyro: np.ndarray              # (n, 3) filtered gyro, deg/s
    gyro_raw: np.ndarray | None   # (n, 3) pre-filter gyro, deg/s
    setpoint: np.ndarray | None   # (n, 3) deg/s
    throttle: np.ndarray          # (n,) 0..1
    dterm: np.ndarray | None      # (n, 3) D term output
    pterm: np.ndarray | None
    iterm: np.ndarray | None
    fterm: np.ndarray | None
    motors: np.ndarray | None     # (n, m) normalised 0..1
    motor_hz: np.ndarray | None   # (n, m) mechanical rotation frequency (Hz)
    airborne: np.ndarray          # (n,) bool mask of "in flight" samples
    notes: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.t)

    @property
    def flight_time_s(self) -> float:
        return float(self.airborne.sum()) / self.fs if self.fs else 0.0


def _resample(t_us: np.ndarray, arr: np.ndarray, t_new_us: np.ndarray) -> np.ndarray:
    if arr.ndim == 1:
        return np.interp(t_new_us, t_us, arr)
    return np.stack([np.interp(t_new_us, t_us, arr[:, k]) for k in range(arr.shape[1])], axis=1)


def prepare(log: FlightLog, trim_s: float = 0.5) -> FlightData:
    notes: list[str] = []
    t_us = log.time_us.astype(np.float64)
    if len(t_us) < 100:
        raise ValueError("log too short for analysis")
    # enforce monotonic time (drop duplicates)
    keep = np.concatenate([[True], np.diff(t_us) > 0])
    frames_idx = np.nonzero(keep)[0]
    t_us = t_us[keep]
    dt = float(np.median(np.diff(t_us)))
    fs = 1e6 / dt
    t_new = np.arange(t_us[0], t_us[-1], dt)

    def col(prefix: str):
        a = log.axis(prefix)
        if a is None:
            return None
        return _resample(t_us, a[frames_idx].astype(np.float64), t_new)

    gyro = col("gyroADC")
    if gyro is None:
        raise ValueError("log has no gyroADC field")
    gyro = gyro[:, :3]
    gyro_raw = col("gyroUnfilt")
    if gyro_raw is not None:
        gyro_raw = gyro_raw[:, :3]
    elif log.header_int("debug_mode") == DEBUG_GYRO_SCALED:
        dbg = col("debug")
        if dbg is not None:
            gyro_raw = dbg[:, :3]
            notes.append("unfiltered gyro taken from debug_mode=GYRO_SCALED")
    if gyro_raw is None:
        notes.append("no unfiltered gyro in log (enable blackbox gyroUnfilt / debug_mode GYRO_SCALED) - "
                     "pre-filter noise analysis limited")

    sp = col("setpoint")
    if sp is not None:
        setpoint = sp[:, :3]
        throttle = sp[:, 3] / 1000.0 if sp.shape[1] > 3 else None
    else:
        setpoint = None
        throttle = None
        notes.append("no setpoint field - step response unavailable")
    if throttle is None:
        rc = col("rcCommand")
        if rc is not None and rc.shape[1] > 3:
            throttle = (rc[:, 3] - 1000.0) / 1000.0
        else:
            throttle = np.zeros(len(t_new))
    throttle = np.clip(throttle, 0.0, 1.0)

    def axis3(prefix):
        a = col(prefix)
        if a is None:
            return None
        if a.shape[1] < 3:
            a = np.concatenate([a, np.zeros((a.shape[0], 3 - a.shape[1]))], axis=1)
        return a[:, :3]

    motors = col("motor")
    motor_norm = None
    if motors is not None:
        lo, hi = (log.header_ints("motorOutput") + [0, 0])[:2]
        if hi <= lo:
            lo, hi = 1000, 2000
        motor_norm = np.clip((motors - lo) / float(hi - lo), 0.0, 1.0)

    motor_hz = None
    erpm = col("eRPM")
    if erpm is not None:
        poles = log.header_int("motor_poles", 14) or 14
        motor_hz = erpm * 100.0 / (poles / 2.0) / 60.0

    # airborne mask: armed & throttle above idle (motors spinning with authority)
    if motor_norm is not None:
        airborne = motor_norm.mean(axis=1) > 0.08
    else:
        airborne = throttle > 0.1
    # smooth mask: fill short gaps, remove short blips (100ms)
    k = max(1, int(0.1 * fs))
    kernel = np.ones(k) / k
    airborne = np.convolve(airborne.astype(float), kernel, mode="same") > 0.5
    trim = int(trim_s * fs)
    if trim and len(airborne) > 2 * trim:
        airborne[:trim] = False
        airborne[-trim:] = False

    return FlightData(
        log=log, fs=fs, t=(t_new - t_new[0]) / 1e6, gyro=gyro, gyro_raw=gyro_raw, setpoint=setpoint,
        throttle=throttle, dterm=axis3("axisD"), pterm=axis3("axisP"), iterm=axis3("axisI"),
        fterm=axis3("axisF"), motors=motor_norm, motor_hz=motor_hz, airborne=airborne, notes=notes,
    )


def segments(mask: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    """Contiguous True runs of at least ``min_len`` samples as (start, end) index pairs."""
    if not len(mask):
        return []
    m = np.concatenate([[False], mask.astype(bool), [False]])
    d = np.diff(m.astype(int))
    starts = np.nonzero(d == 1)[0]
    ends = np.nonzero(d == -1)[0]
    return [(s, e) for s, e in zip(starts, ends) if e - s >= min_len]

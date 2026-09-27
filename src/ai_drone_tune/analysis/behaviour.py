"""Flight behaviour metrics: saturation, propwash, throttle-dependent oscillation."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .flight_data import FlightData, segments


def _bandpass_rms(x: np.ndarray, fs: float, lo: float, hi: float) -> np.ndarray:
    """Per-sample band-limited signal via FFT masking (for windowed RMS)."""
    X = np.fft.rfft(x - x.mean())
    f = np.fft.rfftfreq(len(x), 1.0 / fs)
    X[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(X, len(x))


@dataclass
class BehaviourReport:
    flight_time_s: float
    motor_saturation_pct: float | None     # % of airborne time any motor >= 98 %
    propwash_error_rms: float | None       # deg/s, 20-90 Hz error after throttle chops
    baseline_error_rms: float | None       # deg/s, 20-90 Hz error elsewhere
    high_throttle_osc_ratio: float | None  # 30-150 Hz filtered-gyro energy high/mid throttle
    iterm_saturation_pct: float | None
    throttle_mean: float
    throttle_p90: float

    @property
    def propwash_ratio(self) -> float | None:
        if self.propwash_error_rms is None or not self.baseline_error_rms:
            return None
        return self.propwash_error_rms / self.baseline_error_rms

    def to_dict(self) -> dict:
        d = {k: (None if v is None else round(float(v), 3)) for k, v in self.__dict__.items()}
        pr = self.propwash_ratio
        d["propwash_ratio"] = None if pr is None else round(pr, 2)
        return d


def analyze_behaviour(fd: FlightData) -> BehaviourReport:
    fs = fd.fs
    air = fd.airborne
    n_air = int(air.sum())
    sat = None
    if fd.motors is not None and n_air:
        sat = float((fd.motors[air].max(axis=1) >= 0.98).mean() * 100.0)

    prop = base = None
    if fd.setpoint is not None and n_air > fs * 2:
        err = np.zeros(fd.n)
        for k in (0, 1):
            e = fd.setpoint[:, k] - fd.gyro[:, k]
            err += _bandpass_rms(e, fs, 20, 90) ** 2
        err = np.sqrt(err / 2)
        # throttle chop events: throttle falls > 25 % within 150 ms
        thr = fd.throttle
        lag = int(0.15 * fs)
        drop = np.zeros(fd.n, dtype=bool)
        if fd.n > lag:
            drop[lag:] = (thr[:-lag] - thr[lag:]) > 0.25
        after = np.zeros(fd.n, dtype=bool)
        win = int(0.4 * fs)
        for s, _e in segments(drop, 1):
            after[s:s + win] = True
        after &= air
        rest = air & ~after
        if after.sum() > fs * 0.2 and rest.sum() > fs:
            prop = float(np.sqrt(np.mean(err[after] ** 2)))
            base = float(np.sqrt(np.mean(err[rest] ** 2)))

    osc = None
    if n_air > fs * 3:
        sig = np.zeros(fd.n)
        for k in (0, 1):
            sig += _bandpass_rms(fd.gyro[:, k], fs, 30, 150) ** 2
        high = air & (fd.throttle > 0.7)
        mid = air & (fd.throttle > 0.3) & (fd.throttle < 0.6)
        if high.sum() > fs * 0.5 and mid.sum() > fs * 0.5:
            osc = float(np.sqrt(sig[high].mean() / max(sig[mid].mean(), 1e-9)))

    isat = None
    if fd.iterm is not None and n_air:
        isat = float((np.abs(fd.iterm[air, :2]).max(axis=1) > 400).mean() * 100.0)

    thr_air = fd.throttle[air] if n_air else fd.throttle
    return BehaviourReport(
        flight_time_s=n_air / fs if fs else 0.0,
        motor_saturation_pct=sat,
        propwash_error_rms=prop,
        baseline_error_rms=base,
        high_throttle_osc_ratio=osc,
        iterm_saturation_pct=isat,
        throttle_mean=float(thr_air.mean()) if len(thr_air) else 0.0,
        throttle_p90=float(np.percentile(thr_air, 90)) if len(thr_air) else 0.0,
    )

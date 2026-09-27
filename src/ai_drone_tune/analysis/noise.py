"""Gyro / D-term noise analysis."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .flight_data import AXIS_NAMES, FlightData
from .spectrum import Peak, _moving_median, band_rms, find_peaks, throttle_spectrogram, to_db, welch, xcorr_delay


@dataclass
class AxisNoise:
    axis: str
    raw_hf_rms: float | None        # pre-filter gyro, >100 Hz, deg/s
    filtered_hf_rms: float          # post-filter gyro, >100 Hz, deg/s
    filtered_mid_rms: float         # post-filter gyro, 20-100 Hz (propwash / oscillation band)
    dterm_hf_pct: float | None      # D-term >80 Hz as % of full PID output range
    attenuation_db: float | None    # raw vs filtered, >100 Hz
    filter_delay_ms: float | None
    raw_peaks: list[Peak] = field(default_factory=list)
    filtered_peaks: list[Peak] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "axis": self.axis,
            "raw_hf_rms": _r(self.raw_hf_rms),
            "filtered_hf_rms": _r(self.filtered_hf_rms),
            "filtered_mid_rms": _r(self.filtered_mid_rms),
            "dterm_hf_pct": _r(self.dterm_hf_pct),
            "attenuation_db": _r(self.attenuation_db),
            "filter_delay_ms": _r(self.filter_delay_ms),
            "raw_peaks": [p.to_dict() for p in self.raw_peaks],
            "filtered_peaks": [p.to_dict() for p in self.filtered_peaks],
        }


def _r(v, nd=2):
    return None if v is None else round(float(v), nd)


@dataclass
class MotorNoiseInfo:
    fundamental_hz_p10: float
    fundamental_hz_p50: float
    fundamental_hz_p90: float
    rpm_filter_attenuation_db: float | None  # raw vs filtered energy tracked at motor fundamental

    def to_dict(self) -> dict:
        return {k: _r(v, 1) for k, v in self.__dict__.items()}


@dataclass
class NoiseReport:
    axes: list[AxisNoise]
    motor: MotorNoiseInfo | None
    resonances: list[Peak]          # throttle-independent peaks (frame / props)
    motor_lines: list[dict]         # throttle-dependent lines: {"slope_hz_per_throttle", "intercept"}
    spectra: dict                   # for plotting: {"freqs", "raw": [..], "filtered": [..], "dterm": [..]}
    notes: list[str] = field(default_factory=list)

    def axis(self, name: str) -> AxisNoise:
        return next(a for a in self.axes if a.axis == name)

    def to_dict(self) -> dict:
        return {
            "axes": [a.to_dict() for a in self.axes],
            "motor": self.motor.to_dict() if self.motor else None,
            "resonances": [p.to_dict() for p in self.resonances],
            "motor_lines": self.motor_lines,
            "notes": self.notes,
        }


def _classify_peaks(fd: FlightData, sig: np.ndarray, peaks: list[Peak]) -> tuple[list[Peak], list[dict]]:
    """Tell throttle-dependent (motor) peaks from fixed-frequency (frame resonance) peaks
    using a throttle-binned spectrogram (and the eRPM telemetry when available)."""
    n_bins = 10
    spec = throttle_spectrogram(sig, fd.throttle, fd.fs, fd.airborne, nperseg=256, n_bins=n_bins)
    valid = spec.counts >= 3
    resonances: list[Peak] = []
    lines: list[dict] = []
    if valid.sum() < 3:
        return [], []
    freqs = spec.freqs
    df = freqs[1] - freqs[0]
    db = to_db(np.nan_to_num(spec.psd[valid], nan=1e-12))
    thr = spec.throttle_bins[valid]
    width = max(3, int(120.0 / df))
    base = np.stack([_moving_median(row, width) for row in db])
    prom = db - base

    motor_bin_hz = None
    if fd.motor_hz is not None:
        mh = fd.motor_hz.mean(axis=1)
        bins = np.minimum((fd.throttle * n_bins).astype(int), n_bins - 1)
        motor_bin_hz = np.array([np.median(mh[fd.airborne & (bins == b)]) if np.any(fd.airborne & (bins == b))
                                 else np.nan for b in range(n_bins)])[valid]

    for p in peaks:
        fi = int(round(p.freq / df))
        if fi >= db.shape[1]:
            continue
        lo, hi = max(0, fi - 1), min(db.shape[1], fi + 2)
        present = prom[:, lo:hi].max(axis=1) > 4.0
        strongest = int(np.argmax(db[:, lo:hi].max(axis=1)))
        is_motor = False
        if motor_bin_hz is not None:
            mf = motor_bin_hz[strongest]
            if np.isfinite(mf) and mf > 0:
                is_motor = any(abs(p.freq - h * mf) <= 0.12 * h * mf for h in (1, 2, 3))
        else:
            band = (freqs >= p.freq * 0.6) & (freqs <= p.freq * 1.4)
            f_track = freqs[band][np.argmax(db[:, band], axis=1)]
            if np.std(f_track) > 0:
                corr = float(np.corrcoef(thr, f_track)[0, 1])
                if corr > 0.7 and np.std(f_track) / p.freq > 0.08:
                    is_motor = True
                    slope, intercept = np.polyfit(thr, f_track, 1)
                    lines.append({"slope_hz_per_throttle": round(float(slope), 1),
                                  "intercept_hz": round(float(intercept), 1)})
        if present.mean() >= 0.6 and not (is_motor and present.mean() < 0.9):
            p.kind = "frame_resonance"
            resonances.append(p)
        elif is_motor:
            p.kind = "motor"
    return resonances, lines


def analyze_noise(fd: FlightData) -> NoiseReport:
    notes: list[str] = []
    fs = fd.fs
    nyq = fs / 2
    mask = fd.airborne
    if mask.sum() < fs * 2:
        notes.append("less than 2 s of flight data - noise analysis unreliable")
        mask = np.ones_like(mask)
    axes: list[AxisNoise] = []
    spectra = {"freqs": None, "raw": [], "filtered": [], "dterm": []}
    for k, name in enumerate(AXIS_NAMES):
        f, p_filt = welch(fd.gyro[:, k], fs, mask=mask)
        spectra["freqs"] = f
        spectra["filtered"].append(p_filt)
        hf_hi = min(nyq, 1000.0)
        filtered_hf = band_rms(f, p_filt, 100, hf_hi)
        filtered_mid = band_rms(f, p_filt, 20, 100)
        raw_hf = att = delay = None
        raw_peaks: list[Peak] = []
        if fd.gyro_raw is not None:
            _, p_raw = welch(fd.gyro_raw[:, k], fs, mask=mask)
            spectra["raw"].append(p_raw)
            raw_hf = band_rms(f, p_raw, 100, hf_hi)
            if raw_hf > 0 and filtered_hf > 0:
                att = 20 * np.log10(filtered_hf / raw_hf)
            raw_peaks = find_peaks(f, p_raw, fmin=60)
            d = xcorr_delay(fd.gyro_raw[:, k], fd.gyro[:, k], fs, 0.02, mask)
            delay = d * 1000 if d is not None else None
        dterm_pct = None
        if fd.dterm is not None and k < 2:
            _, p_d = welch(fd.dterm[:, k], fs, mask=mask)
            spectra["dterm"].append(p_d)
            dterm_pct = band_rms(f, p_d, 80, hf_hi) / 1000.0 * 100.0
        axes.append(AxisNoise(name, raw_hf, filtered_hf, filtered_mid, dterm_pct, att, delay,
                              raw_peaks, find_peaks(f, p_filt, fmin=60)))

    # peak classification (roll + pitch): raw gyro shows what the filters face,
    # filtered gyro shows what is left over for the dynamic notch / lowpass to handle
    resonances: list[Peak] = []
    motor_lines: list[dict] = []
    for attr, peaks_attr in (("gyro_raw", "raw_peaks"), ("gyro", "filtered_peaks")):
        src = getattr(fd, attr)
        if src is None:
            continue
        cand: list[Peak] = []
        for ax in axes[:2]:
            for p in sorted(getattr(ax, peaks_attr), key=lambda p: -p.prominence_db):
                if all(abs(p.freq - u.freq) > 15 for u in cand):
                    cand.append(Peak(p.freq, p.level_db, p.prominence_db))
        if not cand:
            continue
        res, lines = _classify_peaks(fd, src[:, 0] + src[:, 1], cand)
        motor_lines.extend(lines)
        for r in res:
            if all(abs(r.freq - u.freq) > 15 for u in resonances):
                resonances.append(r)
        for ax in axes:
            for p in getattr(ax, peaks_attr):
                for u in cand:
                    if abs(u.freq - p.freq) <= 15:
                        p.kind = u.kind
    resonances.sort(key=lambda p: p.freq)

    motor = None
    if fd.motor_hz is not None:
        mh = fd.motor_hz[mask].mean(axis=1) if mask.any() else fd.motor_hz.mean(axis=1)
        mh = mh[mh > 5]
        if len(mh):
            rpm_att = _rpm_tracked_attenuation(fd) if fd.gyro_raw is not None else None
            motor = MotorNoiseInfo(float(np.percentile(mh, 10)), float(np.percentile(mh, 50)),
                                   float(np.percentile(mh, 90)), rpm_att)
    notes.extend(fd.notes)
    return NoiseReport(axes, motor, resonances, motor_lines, spectra, notes)


def _rpm_tracked_attenuation(fd: FlightData, nperseg: int = 256) -> float | None:
    """Energy at the instantaneous motor fundamental, raw vs filtered gyro (dB)."""
    step = nperseg // 2
    win = np.hanning(nperseg)
    freqs = np.fft.rfftfreq(nperseg, 1.0 / fd.fs)
    raw_e = filt_e = 0.0
    for st in range(0, fd.n - nperseg, step):
        if not fd.airborne[st:st + nperseg].all():
            continue
        mf = fd.motor_hz[st:st + nperseg].mean()
        if mf < 30 or mf > fd.fs / 2 * 0.9:
            continue
        band = np.abs(freqs - mf) <= max(10.0, mf * 0.08)
        for k in (0, 1):
            r = np.abs(np.fft.rfft(fd.gyro_raw[st:st + nperseg, k] * win)) ** 2
            f = np.abs(np.fft.rfft(fd.gyro[st:st + nperseg, k] * win)) ** 2
            raw_e += r[band].sum()
            filt_e += f[band].sum()
    if raw_e <= 0 or filt_e <= 0:
        return None
    return float(10 * np.log10(filt_e / raw_e))

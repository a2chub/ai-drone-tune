"""Spectral analysis helpers (numpy only)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .flight_data import segments


def _next_pow2(n: int) -> int:
    return 1 << max(4, int(np.ceil(np.log2(max(n, 16)))))


def welch(x: np.ndarray, fs: float, nperseg: int | None = None, mask: np.ndarray | None = None,
          overlap: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    """Welch PSD (amplitude^2/Hz) averaged over windows fully inside ``mask``."""
    if nperseg is None:
        nperseg = _next_pow2(int(fs / 2))
    step = max(1, int(nperseg * (1 - overlap)))
    win = np.hanning(nperseg)
    scale = 1.0 / (fs * (win ** 2).sum())
    acc = np.zeros(nperseg // 2 + 1)
    count = 0
    ranges = segments(mask, nperseg) if mask is not None else [(0, len(x))]
    for s, e in ranges:
        for st in range(s, e - nperseg + 1, step):
            seg = x[st:st + nperseg]
            seg = seg - seg.mean()
            spec = np.fft.rfft(seg * win)
            acc += (np.abs(spec) ** 2) * scale
            count += 1
    freqs = np.fft.rfftfreq(nperseg, 1.0 / fs)
    if count == 0:
        return freqs, acc
    psd = acc / count
    psd[1:-1] *= 2  # one-sided
    return freqs, psd


def band_rms(freqs: np.ndarray, psd: np.ndarray, lo: float, hi: float) -> float:
    m = (freqs >= lo) & (freqs < hi)
    if not m.any():
        return 0.0
    df = freqs[1] - freqs[0]
    return float(np.sqrt(psd[m].sum() * df))


def to_db(psd: np.ndarray) -> np.ndarray:
    return 10 * np.log10(np.maximum(psd, 1e-12))


def _moving_median(x: np.ndarray, width: int) -> np.ndarray:
    width = max(3, width | 1)
    pad = width // 2
    xp = np.pad(x, pad, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(xp, width)
    return np.median(windows, axis=1)


@dataclass
class Peak:
    freq: float
    level_db: float
    prominence_db: float
    kind: str = "unknown"  # motor / frame_resonance / unknown / electrical

    def to_dict(self) -> dict:
        return {"freq": round(self.freq, 1), "level_db": round(self.level_db, 1),
                "prominence_db": round(self.prominence_db, 1), "kind": self.kind}


def find_peaks(freqs: np.ndarray, psd: np.ndarray, fmin: float = 50.0, fmax: float | None = None,
               min_prominence_db: float = 6.0, max_peaks: int = 6) -> list[Peak]:
    db = to_db(psd)
    df = freqs[1] - freqs[0] if len(freqs) > 1 else 1.0
    baseline = _moving_median(db, int(80.0 / df))
    prom = db - baseline
    fmax = fmax or freqs[-1] * 0.95
    peaks: list[Peak] = []
    for i in range(1, len(db) - 1):
        f = freqs[i]
        if f < fmin or f > fmax:
            continue
        if db[i] >= db[i - 1] and db[i] > db[i + 1] and prom[i] >= min_prominence_db:
            peaks.append(Peak(float(f), float(db[i]), float(prom[i])))
    # merge peaks closer than 15 Hz, keep the strongest
    peaks.sort(key=lambda p: -p.prominence_db)
    kept: list[Peak] = []
    for p in peaks:
        if all(abs(p.freq - k.freq) > 15 for k in kept):
            kept.append(p)
    return sorted(kept[:max_peaks], key=lambda p: p.freq)


@dataclass
class ThrottleSpectrogram:
    throttle_bins: np.ndarray      # bin centres 0..1
    freqs: np.ndarray
    psd: np.ndarray                # (n_bins, n_freqs), NaN where no data
    counts: np.ndarray             # windows per bin


def throttle_spectrogram(x: np.ndarray, throttle: np.ndarray, fs: float, mask: np.ndarray | None = None,
                         nperseg: int = 256, n_bins: int = 10) -> ThrottleSpectrogram:
    step = nperseg // 2
    win = np.hanning(nperseg)
    scale = 1.0 / (fs * (win ** 2).sum())
    freqs = np.fft.rfftfreq(nperseg, 1.0 / fs)
    acc = np.zeros((n_bins, len(freqs)))
    counts = np.zeros(n_bins, dtype=int)
    ranges = segments(mask, nperseg) if mask is not None else [(0, len(x))]
    for s, e in ranges:
        for st in range(s, e - nperseg + 1, step):
            seg = x[st:st + nperseg]
            thr = float(throttle[st:st + nperseg].mean())
            b = min(n_bins - 1, int(thr * n_bins))
            spec = np.abs(np.fft.rfft((seg - seg.mean()) * win)) ** 2 * scale
            acc[b] += spec
            counts[b] += 1
    with np.errstate(invalid="ignore", divide="ignore"):
        psd = acc / counts[:, None]
    psd[counts == 0] = np.nan
    centres = (np.arange(n_bins) + 0.5) / n_bins
    return ThrottleSpectrogram(centres, freqs, psd, counts)


def xcorr_delay(a: np.ndarray, b: np.ndarray, fs: float, max_lag_s: float = 0.03,
                mask: np.ndarray | None = None) -> float | None:
    """Delay (seconds) by which ``b`` lags ``a``; None if correlation is too weak."""
    if mask is not None:
        idx = np.nonzero(mask)[0]
        if len(idx) < fs:
            return None
        a = a[idx[0]:idx[-1]]
        b = b[idx[0]:idx[-1]]
    a = a - a.mean()
    b = b - b.mean()
    n = len(a)
    if n < 64:
        return None
    nfft = _next_pow2(2 * n)
    fa = np.fft.rfft(a, nfft)
    fb = np.fft.rfft(b, nfft)
    cc = np.fft.irfft(np.conj(fa) * fb, nfft)
    max_lag = int(max_lag_s * fs)
    lags = cc[: max_lag + 1]
    k = int(np.argmax(lags))
    norm = np.sqrt((a ** 2).sum() * (b ** 2).sum())
    if norm <= 0 or lags[k] / norm < 0.3:
        return None
    # parabolic interpolation
    if 0 < k < max_lag:
        y0, y1, y2 = lags[k - 1], lags[k], lags[k + 1]
        denom = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / denom if denom != 0 else 0.0
    else:
        frac = 0.0
    return (k + frac) / fs

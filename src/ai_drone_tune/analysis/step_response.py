"""Step response estimation via Wiener deconvolution (setpoint -> gyro).

Approach follows Plasmatree PID-Analyzer: the flight is cut into overlapping
windows, the impulse response of each window is obtained by regularised
deconvolution, integrated to a step response and the good windows are
averaged.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .flight_data import AXIS_NAMES, FlightData


@dataclass
class StepResponse:
    axis: str
    t_ms: np.ndarray
    response: np.ndarray
    windows_used: int
    peak: float
    overshoot_pct: float
    steady: float
    delay_ms: float        # time to 50 %
    rise_ms: float         # 10 % -> 90 %
    settle_ms: float       # last excursion outside +-5 %
    ringing: int           # number of oscillation half-cycles > 5 % after the peak
    tracking_lag_ms: float | None

    def to_dict(self) -> dict:
        return {
            "axis": self.axis, "windows_used": self.windows_used,
            "peak": round(self.peak, 3), "overshoot_pct": round(self.overshoot_pct, 1),
            "steady": round(self.steady, 3), "delay_ms": round(self.delay_ms, 1),
            "rise_ms": round(self.rise_ms, 1), "settle_ms": round(self.settle_ms, 1),
            "ringing": self.ringing,
            "tracking_lag_ms": None if self.tracking_lag_ms is None else round(self.tracking_lag_ms, 1),
        }


def _gauss_smooth(x: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return x
    r = int(3 * sigma) + 1
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    return np.convolve(np.pad(x, r, mode="edge"), k, mode="valid")


def _wiener(inp: np.ndarray, out: np.ndarray, fs: float, cutfreq: float = 25.0) -> np.ndarray:
    n = inp.shape[-1]
    nfft = 1 << int(np.ceil(np.log2(n + 1024)))
    H = np.fft.fft(inp, nfft, axis=-1)
    G = np.fft.fft(out, nfft, axis=-1)
    freq = np.abs(np.fft.fftfreq(nfft, 1.0 / fs))
    sn = (freq >= cutfreq).astype(float)  # 0 below cut-off, 1 above
    len_lpf = float((1 - sn).sum())
    sn = np.clip(_gauss_smooth(sn, len_lpf / 6.0), 0, 1)
    sn = 10.0 * (-sn + 1.0 + 1e-9)
    Hc = np.conj(H)
    return np.real(np.fft.ifft(G * Hc / (H * Hc + 1.0 / sn), axis=-1))


def estimate_step_response(fd: FlightData, axis: int, window_s: float = 1.0, resp_s: float = 0.5,
                           min_input: float = 20.0) -> StepResponse | None:
    if fd.setpoint is None:
        return None
    fs = fd.fs
    wlen = int(window_s * fs)
    rlen = int(resp_s * fs)
    step = max(1, wlen // 8)
    sp = fd.setpoint[:, axis]
    gy = fd.gyro[:, axis]
    tukey = np.hanning(wlen)
    inputs, outputs = [], []
    for st in range(0, fd.n - wlen, step):
        if not fd.airborne[st:st + wlen].all():
            continue
        s = sp[st:st + wlen]
        if np.max(np.abs(s)) < min_input:
            continue
        inputs.append(s * tukey)
        outputs.append(gy[st:st + wlen] * tukey)
    if len(inputs) < 3:
        return None
    inputs = np.array(inputs)
    outputs = np.array(outputs)
    impulses = _wiener(inputs, outputs, fs)[:, :rlen]
    steps = np.cumsum(impulses, axis=1)
    t = np.arange(rlen) / fs * 1000.0
    late = (t >= 200) & (t <= 400)
    steady_each = steps[:, late].mean(axis=1)
    good = (steady_each > 0.5) & (steady_each < 1.5) & (np.max(steps, axis=1) < 3.0)
    if good.sum() < 3:
        return None
    # weight windows by input energy (strong stick inputs give cleaner estimates)
    weights = np.sqrt((inputs[good] ** 2).mean(axis=1))
    resp = (steps[good] * weights[:, None]).sum(axis=0) / weights.sum()
    return _metrics(AXIS_NAMES[axis], t, resp, int(good.sum()), _tracking_lag(fd, axis))


def _tracking_lag(fd: FlightData, axis: int) -> float | None:
    from .spectrum import xcorr_delay

    if fd.setpoint is None:
        return None
    d = xcorr_delay(fd.setpoint[:, axis], fd.gyro[:, axis], fd.fs, 0.08, fd.airborne)
    return None if d is None else d * 1000.0


def _metrics(axis: str, t: np.ndarray, resp: np.ndarray, n: int, lag) -> StepResponse:
    late = (t >= 200) & (t <= 400)
    steady = float(resp[late].mean())
    norm = resp / steady if steady else resp
    early = t <= 200
    peak = float(norm[early].max())
    overshoot = max(0.0, (peak - 1.0) * 100.0)

    def first_cross(level):
        idx = np.nonzero(norm >= level)[0]
        return float(t[idx[0]]) if len(idx) else float(t[-1])

    t10, t50, t90 = first_cross(0.1), first_cross(0.5), first_cross(0.9)
    outside = np.nonzero(np.abs(norm - 1.0) > 0.05)[0]
    settle = float(t[outside[-1]]) if len(outside) else 0.0
    ipk = int(np.argmax(norm[early]))
    tail = norm[ipk:] - 1.0
    ringing = 0
    sign = 0
    for v in tail:
        s = 1 if v > 0.05 else (-1 if v < -0.05 else 0)
        if s and s != sign:
            ringing += 1
            sign = s
    return StepResponse(axis, t, resp, n, peak, overshoot, steady, t50, t90 - t10, settle,
                        max(0, ringing - 1), lag)


def analyze_step_responses(fd: FlightData) -> list[StepResponse]:
    out = []
    for k in range(3):
        sr = estimate_step_response(fd, k)
        if sr is not None:
            out.append(sr)
    return out

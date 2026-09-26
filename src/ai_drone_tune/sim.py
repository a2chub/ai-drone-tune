"""Tiny quad-axis simulator that writes Betaflight-format blackbox logs.

It is *not* a physics-accurate model; it exists so the analysis and tuning
pipeline can be exercised end-to-end without hardware (``aidt simulate``) and
so tests can check that recommendations move in the right direction
(e.g. an under-damped tune gets more D / less P).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .blackbox.writer import BlackboxWriter, betaflight_main_fields


@dataclass
class SimConfig:
    duration_s: float = 30.0
    loop_hz: int = 4000
    log_denom: int = 2
    p: tuple = (45, 47, 45)
    i: tuple = (80, 84, 80)
    d: tuple = (40, 46, 0)
    f: tuple = (120, 125, 120)
    gyro_lpf_hz: float = 250.0
    dterm_lpf_hz: float = 100.0
    motor_noise: float = 25.0        # deg/s amplitude of motor noise at full throttle
    rpm_filter: bool = True
    resonance_hz: float | None = 180.0
    resonance_amp: float = 4.0
    white_noise: float = 0.4
    motor_poles: int = 14
    motor_kv_hz_full: float = 420.0  # motor fundamental (Hz) at full throttle
    plant_gain: float = 66.0         # (deg/s^2) per PID output unit
    motor_tau_s: float = 0.012
    seed: int = 1
    craft_name: str = "SIM"
    extra_headers: dict = field(default_factory=dict)


def _pt1_alpha(cut_hz: float, dt: float) -> float:
    rc = 1.0 / (2 * np.pi * cut_hz)
    return dt / (rc + dt)


def _stick_profile(n: int, fs: float, rng: np.random.Generator) -> np.ndarray:
    """Pilot-like roll/pitch/yaw rate commands (deg/s): holds, snaps, flips."""
    sp = np.zeros((n, 3))
    t = 0
    while t < n:
        hold = int(rng.uniform(0.15, 0.6) * fs)
        for ax in range(3):
            if rng.random() < 0.55:
                amp = rng.choice([-1, 1]) * rng.uniform(80, 650 if ax < 2 else 300)
            else:
                amp = rng.normal(0, 25)
            sp[t:t + hold, ax] = amp
        t += hold
    # rc smoothing
    a = _pt1_alpha(30.0, 1.0 / fs)
    out = np.zeros_like(sp)
    acc = np.zeros(3)
    for k in range(n):
        acc += a * (sp[k] - acc)
        out[k] = acc
    return out


def _throttle_profile(n: int, fs: float, rng: np.random.Generator) -> np.ndarray:
    thr = np.zeros(n)
    t = 0
    level = 0.35
    while t < n:
        hold = int(rng.uniform(0.3, 1.5) * fs)
        r = rng.random()
        if r < 0.2:
            level = rng.uniform(0.75, 0.9)   # punch
        elif r < 0.4:
            level = rng.uniform(0.05, 0.15)  # chop (propwash)
        else:
            level = rng.uniform(0.25, 0.6)
        thr[t:t + hold] = level
        t += hold
    a = _pt1_alpha(8.0, 1.0 / fs)
    out = np.zeros(n)
    acc = 0.35
    for k in range(n):
        acc += a * (thr[k] - acc)
        out[k] = acc
    return out


def simulate(cfg: SimConfig) -> bytes:
    rng = np.random.default_rng(cfg.seed)
    fs = cfg.loop_hz
    dt = 1.0 / fs
    n = int(cfg.duration_s * fs)
    arm = int(1.0 * fs)  # 1 s on the ground at idle before takeoff
    sp = _stick_profile(n, fs, rng)
    thr = _throttle_profile(n, fs, rng)
    sp[:arm] = 0
    thr[:arm] = 0.0

    motor_hz = 60 + thr * (cfg.motor_kv_hz_full - 60)
    phase = np.cumsum(2 * np.pi * motor_hz * dt)
    motor_noise = cfg.motor_noise * (0.2 + thr)[:, None] * np.stack(
        [np.sin(phase + ax) + 0.4 * np.sin(2 * phase + 2 * ax) for ax in range(3)], axis=1)
    if cfg.resonance_hz:
        res_phase = 2 * np.pi * cfg.resonance_hz * np.arange(n) * dt
        env = cfg.resonance_amp * (0.5 + thr)
        resonance = env[:, None] * np.stack([np.sin(res_phase + rng.uniform(0, 6)) for _ in range(3)], axis=1)
    else:
        resonance = np.zeros((n, 3))

    a_gyro = _pt1_alpha(cfg.gyro_lpf_hz, dt)
    a_dterm = _pt1_alpha(cfg.dterm_lpf_hz, dt)
    a_motor = dt / (cfg.motor_tau_s + dt)

    rate = np.zeros(3)
    act = np.zeros(3)
    gf = np.zeros(3)
    gf2 = np.zeros(3)
    dstate = np.zeros(3)
    prev_gf = np.zeros(3)
    prev_sp = np.zeros(3)
    sp_lpf = np.zeros(3)
    a_relax = _pt1_alpha(15.0, dt)
    iterm = np.zeros(3)
    # Betaflight PID term scales (pid.c): PTERM 0.032029, ITERM 0.244381, DTERM 0.000529
    P = np.array(cfg.p, float) * 0.032029
    I = np.array(cfg.i, float) * 0.244381
    D = np.array(cfg.d, float) * 0.000529
    F = np.array(cfg.f, float) * 0.00007
    axis_gain = np.array([1.0, 0.95, 0.45]) * cfg.plant_gain
    rpm_att = 0.08 if cfg.rpm_filter else 1.0
    rows_raw = np.zeros((n, 3))
    rows_gyro = np.zeros((n, 3))
    rows_p = np.zeros((n, 3))
    rows_i = np.zeros((n, 3))
    rows_d = np.zeros((n, 3))
    rows_f = np.zeros((n, 3))
    rows_u = np.zeros((n, 3))
    white = rng.normal(0, cfg.white_noise, (n, 3))
    for k in range(n):
        noise_total = motor_noise[k] + resonance[k] + white[k]
        raw = rate + noise_total
        # filters: RPM filter removes most motor noise, then 2x PT1 (≈ lpf1 + lpf2)
        filt_in = rate + motor_noise[k] * rpm_att + resonance[k] + white[k]
        gf += a_gyro * (filt_in - gf)
        gf2 += a_gyro * (gf - gf2)
        g = gf2
        err = sp[k] - g
        pterm = P * err
        # iterm_relax (RP, setpoint mode): suppress I accumulation during fast stick moves
        sp_lpf += a_relax * (sp[k] - sp_lpf)
        relax = np.clip(1.0 - np.abs(sp[k] - sp_lpf) / 40.0, 0.0, 1.0)
        relax[2] = 1.0
        iterm = np.clip(iterm + I * err * relax * dt, -250, 250)
        dstate += a_dterm * ((-(g - prev_gf) * fs) - dstate)
        dterm = D * dstate
        fterm = F * (sp[k] - prev_sp) * fs
        prev_gf = g.copy()
        prev_sp = sp[k].copy()
        u = np.clip(pterm + iterm + dterm + fterm, -500, 500)
        if k < arm:
            u[:] = 0
            iterm[:] = 0
        act += a_motor * (u - act)
        accel = axis_gain * act - 0.5 * rate
        rate = rate + accel * dt
        rows_raw[k] = raw
        rows_gyro[k] = g
        rows_p[k], rows_i[k], rows_d[k], rows_f[k], rows_u[k] = pterm, iterm, dterm, fterm, u

    # --- write log at fs / log_denom ------------------------------------
    fields = betaflight_main_fields(motors=4, gyro_unfilt=True, erpm=True, debug=False)
    names = [f.name for f in fields]
    headers = {
        "Firmware type": "Cleanflight",
        "Firmware revision": "Betaflight 4.5.1 (77d01ba3b) STM32F7X2",
        "Craft name": cfg.craft_name,
        "looptime": str(int(1e6 / fs)),
        "pid_process_denom": "1",
        "gyro_scale": "0x3f800000",
        "motor_poles": str(cfg.motor_poles),
        "dshot_bidir": "1" if cfg.rpm_filter else "0",
        "rpm_filter_harmonics": "3" if cfg.rpm_filter else "0",
        "rollPID": f"{cfg.p[0]},{cfg.i[0]},{cfg.d[0]}",
        "pitchPID": f"{cfg.p[1]},{cfg.i[1]},{cfg.d[1]}",
        "yawPID": f"{cfg.p[2]},{cfg.i[2]},{cfg.d[2]}",
        "ff_weight": ",".join(str(x) for x in cfg.f),
        "gyro_lpf1_static_hz": str(int(cfg.gyro_lpf_hz)),
        "gyro_lpf2_static_hz": str(int(cfg.gyro_lpf_hz * 2)),
        "dterm_lpf1_static_hz": str(int(cfg.dterm_lpf_hz)),
        "debug_mode": "0",
    }
    headers.update(cfg.extra_headers)
    w = BlackboxWriter(fields, headers, i_interval=32, minmotor=48, vbatref=1650)
    w.write_header()
    idx = {name: i for i, name in enumerate(names)}
    t0 = 5_000_000
    log_i = 0
    for k in range(0, n, cfg.log_denom):
        row = [0] * len(names)
        row[idx["loopIteration"]] = log_i
        row[idx["time"]] = t0 + int(k * dt * 1e6)
        for ax in range(3):
            row[idx[f"axisP[{ax}]"]] = int(rows_p[k, ax])
            row[idx[f"axisI[{ax}]"]] = int(rows_i[k, ax])
            if f"axisD[{ax}]" in idx:
                row[idx[f"axisD[{ax}]"]] = int(rows_d[k, ax])
            row[idx[f"axisF[{ax}]"]] = int(rows_f[k, ax])
            row[idx[f"rcCommand[{ax}]"]] = int(sp[k, ax] / 2)
            row[idx[f"setpoint[{ax}]"]] = int(round(sp[k, ax]))
            row[idx[f"gyroADC[{ax}]"]] = int(round(rows_gyro[k, ax]))
            row[idx[f"gyroUnfilt[{ax}]"]] = int(round(rows_raw[k, ax]))
            row[idx[f"accSmooth[{ax}]"]] = 0 if ax < 2 else 2048
        row[idx["rcCommand[3]"]] = int(1000 + thr[k] * 1000)
        row[idx["setpoint[3]"]] = int(thr[k] * 1000)
        row[idx["vbatLatest"]] = 1600
        row[idx["amperageLatest"]] = int(thr[k] * 6000)
        row[idx["rssi"]] = 1023
        mix = [(-1, 1, -1), (-1, -1, 1), (1, 1, 1), (1, -1, -1)]
        for m in range(4):
            if k < arm:
                mv = 48 if thr[k] <= 0 else 48 + 100
            else:
                uu = sum(mix[m][a] * rows_u[k, a] for a in range(3)) / 1000.0
                mv = 48 + (0.06 + thr[k] * 0.9 + uu) * (2047 - 48)
            row[idx[f"motor[{m}]"]] = int(np.clip(mv, 48, 2047))
            erpm = motor_hz[k] * 60 * (cfg.motor_poles / 2) / 100 if k >= arm else 0
            row[idx[f"eRPM[{m}]"]] = int(erpm)
        w.write_main(row)
        if log_i % 256 == 0:
            w.write_slow([1, 0, 0, 1, 1])
        log_i += 1
    w.write_event_disarm()
    w.write_event_log_end()
    return w.getvalue()

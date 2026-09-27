"""Rule-based tuning recommendations from blackbox analysis.

Design principles

* small steps: every setting moves at most ``max_step`` (default 15 %) per
  iteration - tune, fly, re-analyse, repeat;
* noise first: filters are adjusted before PIDs, and D is only raised when
  the D-term noise budget allows it;
* every change carries a human readable reason and a confidence so the
  approval-gate mode can show *why*;
* hardware-dependent suggestions (e.g. enabling bidirectional DShot) are
  marked advisory and are never applied automatically.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..analysis.report import LogAnalysis
from .changes import Change, ChangeSet
from .config import FCConfig

AXES = ("roll", "pitch", "yaw")


@dataclass
class Thresholds:
    max_step: float = 0.15
    # filtered gyro noise above 100 Hz (deg/s RMS, roll/pitch average)
    gyro_clean: float = 1.0
    gyro_noisy: float = 2.5
    # D-term noise above 80 Hz, % of PID output range
    dterm_clean: float = 1.5
    dterm_noisy: float = 5.0
    dterm_hot: float = 8.0
    # step response
    overshoot_high: float = 15.0
    overshoot_mild: float = 10.0
    overshoot_low: float = 3.0
    rise_slow_ms: float = 30.0
    steady_low: float = 0.90
    ringing_max: int = 2
    tracking_lag_high_ms: float = 25.0
    # behaviour
    propwash_ratio_high: float = 1.4
    high_throttle_osc: float = 1.8
    motor_saturation_high: float = 10.0
    min_flight_s: float = 5.0


def _fmt(v: float) -> str:
    return str(int(round(v)))


class Recommender:
    def __init__(self, cfg: FCConfig, thresholds: Thresholds | None = None):
        self.cfg = cfg
        self.th = thresholds or Thresholds()
        self.cs = ChangeSet()

    # ------------------------------------------------------------------
    def _propose(self, name: str, new: float | int | str, reason: str, category: str,
                 confidence: float, advisory: bool = False, lo: float | None = None,
                 hi: float | None = None) -> None:
        cfg = self.cfg
        old = cfg.get(name)
        if old is None and not advisory:
            return  # setting unknown on this firmware
        pending = self.cs.get(name)
        if isinstance(new, (int, float)) and not isinstance(new, bool):
            if lo is not None or hi is not None:
                new = cfg.clamp(name, float(new), lo if lo is not None else -1e9, hi if hi is not None else 1e9)
            new = _fmt(new)
        if pending is None and old is not None and str(old) == str(new):
            return
        self.cs.add(Change(name, old if pending is None else pending.old, str(new), reason, category,
                           round(confidence, 2), cfg.section_of(name), advisory))

    def _current(self, name: str) -> int | None:
        pending = self.cs.get(name)
        if pending is not None:
            try:
                return int(float(pending.new))
            except ValueError:
                return None
        return self.cfg.get_int(name)

    def _scale(self, name: str, factor: float, reason: str, category: str, confidence: float,
               lo: float = 0, hi: float = 1000, min_delta: int = 1) -> None:
        cur = self._current(name)
        if cur is None or cur == 0:
            return
        factor = float(np.clip(factor, 1 - self.th.max_step, 1 + self.th.max_step))
        new = cur * factor
        if abs(new - cur) < min_delta:
            new = cur + (min_delta if factor > 1 else -min_delta)
        self._propose(name, new, reason, category, confidence, lo=lo, hi=hi)

    def _d_names(self, axis: str) -> list[str]:
        """All D gains that shape the D term on this axis (firmware-version aware)."""
        names = [f"d_{axis}"]
        for extra in (f"d_max_{axis}", f"d_min_{axis}"):
            v = self.cfg.get_int(extra)
            if v:  # 0 means the feature is disabled
                names.append(extra)
        return names

    # ------------------------------------------------------------------
    def recommend(self, analysis: LogAnalysis) -> ChangeSet:
        th = self.th
        a = analysis
        flight_s = a.data.flight_time_s
        conf = float(np.clip(flight_s / 60.0, 0.2, 1.0))
        if flight_s < th.min_flight_s:
            self.cs.notes.append(f"flight time {flight_s:.1f}s < {th.min_flight_s}s: no PID/filter changes proposed")
            return self.cs
        self._filters(a, conf)
        self._pids(a, conf)
        self._behaviour(a, conf)
        self._sliders()
        return self.cs

    # ------------------------------------------------------------------
    def _rpm_active(self, a: LogAnalysis) -> bool:
        return (self.cfg.is_on("dshot_bidir") and (self.cfg.get_int("rpm_filter_harmonics", 0) or 0) > 0
                and a.data.motor_hz is not None)

    def _filters(self, a: LogAnalysis, conf: float) -> None:
        th = self.th
        cfg = self.cfg
        noise = a.noise
        rp = [noise.axis("roll"), noise.axis("pitch")]
        fhf = float(np.mean([x.filtered_hf_rms for x in rp]))
        dterm = [x.dterm_hf_pct for x in rp if x.dterm_hf_pct is not None]
        dterm_pct = float(np.mean(dterm)) if dterm else None
        rpm = self._rpm_active(a)

        # --- bidirectional DShot / RPM filter --------------------------------
        if not cfg.is_on("dshot_bidir"):
            self._propose("dshot_bidir", "ON", "RPM filter removes motor noise with far less delay than "
                          "lowpass filters; requires BLHeli_32 / Bluejay / AM32 ESC firmware",
                          "filter", 0.6, advisory=True)
        elif a.data.motor_hz is None:
            self.cs.notes.append("dshot_bidir is ON but no eRPM in the log - check ESC telemetry / "
                                 "blackbox field settings")

        # --- dynamic notch ----------------------------------------------------
        res = [p for p in noise.resonances if p.freq >= 60]
        n_res = len(res)
        if cfg.has("dyn_notch_count"):
            cur_count = cfg.get_int("dyn_notch_count", 0) or 0
            if rpm:
                target = int(np.clip(n_res, 1, 3))
                why = (f"RPM filter active; {n_res} frame resonance(s) at "
                       f"{', '.join(f'{p.freq:.0f}Hz' for p in res)}" if n_res else
                       "RPM filter active and no frame resonance found - one notch is enough")
            else:
                target = max(3, min(5, n_res + 2))
                why = "no RPM filter - dynamic notches must track motor noise"
            if target != cur_count:
                self._propose("dyn_notch_count", target, why, "filter", conf)
            q_target = 500 if rpm else 300
            cur_q = cfg.get_int("dyn_notch_q")
            if cur_q is not None and abs(cur_q - q_target) > 100:
                self._propose("dyn_notch_q", q_target, "recommended Q for this filter set-up", "filter", conf * 0.8)
        if res and cfg.has("dyn_notch_min_hz"):
            lowest = min(p.freq for p in res)
            highest = max(p.freq for p in res)
            cur_min = cfg.get_int("dyn_notch_min_hz", 100)
            cur_max = cfg.get_int("dyn_notch_max_hz", 600)
            if lowest < cur_min * 1.05:
                self._propose("dyn_notch_min_hz", max(60, int(lowest * 0.85 / 5) * 5),
                              f"resonance at {lowest:.0f}Hz is below/at the notch range", "filter", conf, lo=60, hi=250)
            elif rpm and lowest > cur_min * 1.6 and cur_min < 150:
                self._propose("dyn_notch_min_hz", min(250, int(lowest * 0.8 / 5) * 5),
                              f"lowest resonance {lowest:.0f}Hz - raise notch floor to avoid low-frequency delay",
                              "filter", conf * 0.7, lo=60, hi=250)
            if highest > cur_max * 0.9:
                self._propose("dyn_notch_max_hz", min(1000, int(highest * 1.25 / 10) * 10),
                              f"resonance at {highest:.0f}Hz near the top of the notch range", "filter", conf,
                              lo=200, hi=1000)

        # --- RPM filter tuning -------------------------------------------------
        if rpm and noise.motor is not None:
            att = noise.motor.rpm_filter_attenuation_db
            if att is not None and att > -12 and cfg.has("rpm_filter_q"):
                self._scale("rpm_filter_q", 0.85, f"motor noise only {att:.0f}dB attenuated - widen RPM notches",
                            "filter", conf * 0.7, lo=250, hi=3000)
            p10 = noise.motor.fundamental_hz_p10
            cur_min = cfg.get_int("rpm_filter_min_hz")
            if cur_min and p10 < cur_min * 0.9 and any(
                    p.kind == "motor" and p.freq < cur_min for ax in rp for p in ax.filtered_peaks):
                self._propose("rpm_filter_min_hz", max(50, int(p10 * 0.9)),
                              f"motors spend time at {p10:.0f}Hz, below rpm_filter_min_hz", "filter", conf * 0.7,
                              lo=30, hi=200)

        # --- gyro lowpass ------------------------------------------------------
        gyro_factor = None
        if fhf > th.gyro_noisy:
            gyro_factor = 0.85
            why = f"filtered gyro noise {fhf:.1f} deg/s RMS (>100Hz) is high - lower gyro lowpass"
        elif fhf < th.gyro_clean:
            delays = [x.filter_delay_ms for x in rp if x.filter_delay_ms is not None]
            if not delays or np.mean(delays) > 0.6:
                gyro_factor = 1.15
                why = f"filtered gyro is clean ({fhf:.2f} deg/s) - raise gyro lowpass to cut filter delay"
        if gyro_factor:
            if (cfg.get_int("gyro_lpf1_dyn_min_hz", 0) or 0) > 0:
                self._scale("gyro_lpf1_dyn_min_hz", gyro_factor, why, "filter", conf, lo=100, hi=1000)
                self._scale("gyro_lpf1_dyn_max_hz", gyro_factor, why, "filter", conf, lo=150, hi=1000)
            else:
                self._scale("gyro_lpf1_static_hz", gyro_factor, why, "filter", conf, lo=100, hi=1000)
            self._scale("gyro_lpf2_static_hz", gyro_factor, why, "filter", conf, lo=150, hi=1000)

        # --- D-term lowpass ----------------------------------------------------
        if dterm_pct is not None:
            factor = None
            if dterm_pct > th.dterm_noisy:
                factor = 0.85
                why = f"D-term noise {dterm_pct:.1f}% of output - lower D-term lowpass (hot motors risk)"
            elif dterm_pct < th.dterm_clean and fhf < th.gyro_noisy:
                factor = 1.1
                why = f"D-term is clean ({dterm_pct:.1f}%) - raise D-term lowpass for less delay"
            if factor:
                if (cfg.get_int("dterm_lpf1_dyn_min_hz", 0) or 0) > 0:
                    self._scale("dterm_lpf1_dyn_min_hz", factor, why, "filter", conf, lo=50, hi=300)
                    self._scale("dterm_lpf1_dyn_max_hz", factor, why, "filter", conf, lo=70, hi=400)
                else:
                    self._scale("dterm_lpf1_static_hz", factor, why, "filter", conf, lo=50, hi=300)
                self._scale("dterm_lpf2_static_hz", factor, why, "filter", conf, lo=60, hi=400)

    # ------------------------------------------------------------------
    def _pids(self, a: LogAnalysis, conf: float) -> None:
        th = self.th
        noise = a.noise
        for axis in AXES:
            sr = a.step(axis)
            if sr is None:
                continue
            c = conf * float(np.clip(sr.windows_used / 40.0, 0.3, 1.0))
            dterm = noise.axis(axis).dterm_hf_pct if axis != "yaw" else None
            d_budget_ok = dterm is None or dterm < th.dterm_noisy
            p = f"p_{axis}"
            i = f"i_{axis}"
            f = f"f_{axis}"
            if axis != "yaw":
                if sr.overshoot_pct > th.overshoot_high or sr.ringing >= th.ringing_max:
                    tag = (f"step response overshoot {sr.overshoot_pct:.0f}%"
                           + (f", {sr.ringing} oscillations" if sr.ringing else ""))
                    if d_budget_ok and sr.ringing < th.ringing_max:
                        for dn in self._d_names(axis):
                            self._scale(dn, 1.10, f"{tag} - more D damping", "pid", c, lo=0, hi=250)
                    else:
                        self._scale(p, 0.92, f"{tag} - lower P" + ("" if d_budget_ok else
                                    " (D-term noise leaves no room for more D)"), "pid", c, lo=10, hi=250)
                elif sr.overshoot_pct > th.overshoot_mild and d_budget_ok:
                    for dn in self._d_names(axis):
                        self._scale(dn, 1.05, f"overshoot {sr.overshoot_pct:.0f}% - slightly more D", "pid", c,
                                    lo=0, hi=250)
                elif sr.overshoot_pct < th.overshoot_low and sr.rise_ms > th.rise_slow_ms:
                    self._scale(p, 1.10, f"response is well damped but slow (rise {sr.rise_ms:.0f}ms) - more P",
                                "pid", c, lo=10, hi=250)
                if dterm is not None and dterm > th.dterm_hot and sr.overshoot_pct < th.overshoot_mild:
                    for dn in self._d_names(axis):
                        self._scale(dn, 0.90, f"D-term noise {dterm:.1f}% (motor heat) and little overshoot - "
                                    "lower D", "pid", c, lo=0, hi=250)
            else:
                if sr.overshoot_pct > 25 or sr.ringing >= th.ringing_max:
                    self._scale(p, 0.90, f"yaw overshoot {sr.overshoot_pct:.0f}% - lower yaw P", "pid", c, lo=10,
                                hi=250)
                elif sr.overshoot_pct < th.overshoot_low and sr.rise_ms > th.rise_slow_ms * 1.5:
                    self._scale(p, 1.10, "yaw response slow - more yaw P", "pid", c, lo=10, hi=250)
            if sr.steady < th.steady_low:
                self._scale(i, 1.10, f"step response settles at {sr.steady:.2f} (<{th.steady_low}) - more I",
                            "pid", c * 0.8, lo=10, hi=250)
            lag = sr.tracking_lag_ms
            if lag is not None and self.cfg.has(f):
                if lag > th.tracking_lag_high_ms and sr.overshoot_pct < th.overshoot_mild:
                    self._scale(f, 1.10, f"gyro lags setpoint by {lag:.0f}ms - more feedforward",
                                "feedforward", c * 0.8, lo=0, hi=1000)
                elif lag < 5 and sr.overshoot_pct > th.overshoot_high:
                    self._scale(f, 0.90, f"overshoot with very low lag ({lag:.0f}ms) - less feedforward",
                                "feedforward", c * 0.8, lo=0, hi=1000)

    # ------------------------------------------------------------------
    def _behaviour(self, a: LogAnalysis, conf: float) -> None:
        th = self.th
        b = a.behaviour
        pr = b.propwash_ratio
        if pr is not None and pr > th.propwash_ratio_high:
            rp = [a.noise.axis("roll").dterm_hf_pct, a.noise.axis("pitch").dterm_hf_pct]
            d_ok = all(x is None or x < th.dterm_noisy for x in rp)
            if d_ok:
                for axis in ("roll", "pitch"):
                    # raise the *base* D (d_min before 4.6, d_<axis> from 4.6) for propwash
                    base = f"d_min_{axis}" if self.cfg.get_int(f"d_min_{axis}") else f"d_{axis}"
                    self._scale(base, 1.10, f"propwash: error after throttle chops is {pr:.1f}x normal - more D",
                                "pid", conf * 0.7, lo=0, hi=250)
            if self._rpm_active(a) and (self.cfg.get_int("dyn_idle_min_rpm", 0) or 0) == 0:
                self._propose("dyn_idle_min_rpm", 30, "dynamic idle keeps props loaded in propwash "
                              "(value ~ 3000rpm/100; adjust for prop size)", "other", conf * 0.6)
        osc = b.high_throttle_osc_ratio
        if osc is not None and osc > th.high_throttle_osc and self.cfg.has("tpa_rate"):
            cur = self.cfg.get_int("tpa_rate", 65) or 0
            self._propose("tpa_rate", min(100, cur + 10), f"oscillation at high throttle is {osc:.1f}x "
                          "mid throttle - more TPA", "pid", conf * 0.7, lo=0, hi=100)
            bp = self.cfg.get_int("tpa_breakpoint")
            if bp and bp > 1400:
                self._propose("tpa_breakpoint", bp - 100, "start TPA earlier", "pid", conf * 0.6, lo=1000, hi=2000)
        if b.motor_saturation_pct is not None and b.motor_saturation_pct > th.motor_saturation_high:
            self.cs.notes.append(
                f"motors saturate {b.motor_saturation_pct:.0f}% of flight time - check props/motors or reduce "
                "rates / master multiplier; PID changes cannot fix a lack of authority")

    # ------------------------------------------------------------------
    def _sliders(self) -> None:
        """Keep the Configurator's simplified-tuning sliders from overwriting raw values."""
        cats = {c.category for c in self.cs.applicable()}
        if "pid" in cats or "feedforward" in cats:
            v = self.cfg.get("simplified_pids_mode")
            if v is not None and v.upper() not in ("OFF", "0"):
                self._propose("simplified_pids_mode", "OFF",
                              "raw PID values are tuned directly; sliders would overwrite them", "pid", 1.0)
        if "filter" in cats:
            for name in ("simplified_gyro_filter", "simplified_dterm_filter"):
                v = self.cfg.get(name)
                if v is not None and v.upper() not in ("OFF", "0"):
                    self._propose(name, "OFF", "filter cut-offs are tuned directly; sliders would overwrite them",
                                  "filter", 1.0)


def recommend(analysis: LogAnalysis, cfg: FCConfig, thresholds: Thresholds | None = None) -> ChangeSet:
    return Recommender(cfg, thresholds).recommend(analysis)

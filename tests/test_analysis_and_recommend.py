import numpy as np

from ai_drone_tune.analysis.report import analyze_log
from ai_drone_tune.analysis.spectrum import find_peaks, welch
from ai_drone_tune.blackbox.parser import parse_bytes
from ai_drone_tune.tuning.config import FCConfig, from_blackbox_headers
from ai_drone_tune.tuning.recommender import Recommender, Thresholds


def test_welch_finds_sine():
    fs = 2000
    t = np.arange(20000) / fs
    x = 5 * np.sin(2 * np.pi * 180 * t) + np.random.default_rng(0).normal(0, 1, len(t))
    f, p = welch(x, fs)
    peaks = find_peaks(f, p)
    assert any(abs(pk.freq - 180) < 4 for pk in peaks)


def test_analysis_on_simulated_flight(sim_log_bytes):
    a = analyze_log(parse_bytes(sim_log_bytes)[0])
    assert a.data.flight_time_s > 9
    roll = a.noise.axis("roll")
    assert roll.raw_hf_rms > roll.filtered_hf_rms  # filters attenuate
    assert roll.filter_delay_ms is not None and 0.2 < roll.filter_delay_ms < 3
    assert a.noise.motor is not None and a.noise.motor.fundamental_hz_p50 > 100
    # the simulator puts a fixed 180 Hz frame resonance on top of the motor noise
    assert any(abs(p.freq - 180) < 10 for p in a.noise.resonances)
    steps = {s.axis: s for s in a.steps}
    assert set(steps) == {"roll", "pitch", "yaw"}
    assert 5 < steps["roll"].delay_ms < 40
    assert 0.85 < steps["roll"].steady < 1.15


def test_recommendations_with_rpm_filter(sim_log_bytes):
    lg = parse_bytes(sim_log_bytes)[0]
    a = analyze_log(lg)
    cfg = from_blackbox_headers(lg.headers)
    cfg.values.update({"dyn_notch_count": "3", "dyn_notch_q": "300", "dyn_notch_min_hz": "100",
                       "dyn_notch_max_hz": "600"})
    cs = Recommender(cfg, Thresholds(min_flight_s=5)).recommend(a)
    names = {c.name: c for c in cs.changes}
    assert names["dyn_notch_count"].new == "1"      # RPM filter + one resonance -> 1 notch
    assert "dshot_bidir" not in names
    for c in cs.changes:                             # step limit respected
        try:
            old, new = float(c.old), float(c.new)
        except (TypeError, ValueError):
            continue
        if old and c.name.startswith(("p_", "i_", "d_", "f_", "gyro_", "dterm_")):
            assert abs(new - old) / old <= 0.16


def test_recommendations_without_rpm(sim_log_bytes_norpm):
    lg = parse_bytes(sim_log_bytes_norpm)[0]
    a = analyze_log(lg)
    cfg = from_blackbox_headers(lg.headers)
    cfg.values["dyn_notch_count"] = "1"
    cs = Recommender(cfg, Thresholds(min_flight_s=5)).recommend(a)
    names = {c.name: c for c in cs.changes}
    assert names["dshot_bidir"].advisory
    assert "dshot_bidir" not in {c.name for c in cs.applicable()}
    assert int(names["dyn_notch_count"].new) >= 3
    # noisy filtered gyro -> gyro lowpass lowered
    assert int(names["gyro_lpf1_static_hz"].new) < 250


class _Step:
    def __init__(self, axis, overshoot, rise=20.0, steady=1.0, ringing=0, lag=15.0):
        self.axis, self.overshoot_pct, self.rise_ms, self.steady = axis, overshoot, rise, steady
        self.ringing, self.tracking_lag_ms, self.windows_used = ringing, lag, 100


class _AxisNoise:
    def __init__(self, axis, dterm):
        self.axis, self.dterm_hf_pct, self.filtered_hf_rms = axis, dterm, 1.5
        self.filter_delay_ms, self.filtered_peaks, self.raw_peaks = 1.0, [], []


class _Noise:
    def __init__(self, dterm):
        self.axes = [_AxisNoise(a, dterm if a != "yaw" else None) for a in ("roll", "pitch", "yaw")]
        self.resonances, self.motor = [], None

    def axis(self, name):
        return next(a for a in self.axes if a.axis == name)


class _Beh:
    propwash_ratio = None
    high_throttle_osc_ratio = None
    motor_saturation_pct = 0.0


class _Data:
    flight_time_s = 60.0
    motor_hz = None


class _Analysis:
    def __init__(self, steps, dterm=2.0):
        self.steps, self.noise, self.behaviour, self.data = steps, _Noise(dterm), _Beh(), _Data()

    def step(self, axis):
        return next((s for s in self.steps if s.axis == axis), None)


def _cfg(**extra):
    v = dict(p_roll="45", i_roll="80", d_roll="40", f_roll="120", p_pitch="47", i_pitch="84", d_pitch="46",
             f_pitch="125", p_yaw="45", i_yaw="80", d_yaw="0", f_yaw="120", d_max_roll="0", d_max_pitch="0",
             dshot_bidir="ON", rpm_filter_harmonics="3")
    v.update(extra)
    return FCConfig(values=v)


def test_overshoot_raises_d_including_dmax():
    cfg = _cfg(d_max_roll="55")
    cs = Recommender(cfg).recommend(_Analysis([_Step("roll", 25)]))
    names = {c.name: c.new for c in cs.changes}
    assert int(names["d_roll"]) > 40 and int(names["d_max_roll"]) > 55
    assert "p_roll" not in names


def test_overshoot_with_noisy_dterm_lowers_p():
    cs = Recommender(_cfg()).recommend(_Analysis([_Step("roll", 25)], dterm=7.0))
    names = {c.name: c.new for c in cs.changes}
    assert int(names["p_roll"]) < 45 and "d_roll" not in names


def test_slow_response_raises_p_and_lagging_ff():
    cs = Recommender(_cfg()).recommend(_Analysis([_Step("pitch", 1, rise=45, lag=35)]))
    names = {c.name: c.new for c in cs.changes}
    assert int(names["p_pitch"]) > 47 and int(names["f_pitch"]) > 125


def test_simplified_sliders_turned_off_when_pids_change():
    cs = Recommender(_cfg(simplified_pids_mode="RPY")).recommend(_Analysis([_Step("roll", 25)]))
    assert {c.name: c.new for c in cs.changes}["simplified_pids_mode"] == "OFF"


def test_short_flight_no_changes():
    a = _Analysis([_Step("roll", 40)])
    a.data.flight_time_s = 2
    cs = Recommender(_cfg()).recommend(a)
    assert not cs.changes and cs.notes

import pytest

from ai_drone_tune.tuning.config import FCConfig, SettingSpec
from ai_drone_tune.tuning.instructions import InstructionParser
from ai_drone_tune.tuning.rates import AxisRates, max_rate, rate_at, solve_for_max_rate


def _cfg():
    v = dict(p_roll="45", i_roll="80", d_roll="40", f_roll="120", p_pitch="47", i_pitch="84", d_pitch="46",
             f_pitch="125", p_yaw="45", i_yaw="80", d_yaw="0", f_yaw="120", d_max_roll="0", d_max_pitch="0",
             roll_rc_rate="7", pitch_rc_rate="7", yaw_rc_rate="7", roll_srate="67", pitch_srate="67",
             yaw_srate="67", roll_expo="0", pitch_expo="0", yaw_expo="0", rates_type="ACTUAL",
             dyn_notch_count="3", dshot_bidir="ON", dyn_idle_min_rpm="0", gyro_lpf1_type="PT1")
    cfg = FCConfig(values=v)
    cfg.specs["p_roll"] = SettingSpec("p_roll", "45", "profile", 0, 250)
    cfg.specs["gyro_lpf1_type"] = SettingSpec("gyro_lpf1_type", "PT1", allowed=["PT1", "BIQUAD", "PT2", "PT3"])
    return cfg


def parse(text):
    r = InstructionParser(_cfg()).parse(text)
    return {c.name: c.new for c in r.changes}, r


@pytest.mark.parametrize("text,expected", [
    ("p_roll=52", {"p_roll": "52"}),
    ("set d_pitch = 40", {"d_pitch": "40"}),
    ("d_pitch +10%", {"d_pitch": "51"}),
    ("f_yaw -20", {"f_yaw": "100"}),
    ("gyro_lpf1_type pt2", {"gyro_lpf1_type": "PT2"}),
    ("ロールのPを5%上げて", {"p_roll": "47"}),
    ("ヨーのIを少し上げて", {"i_yaw": "84"}),
    ("pitch D down 10%", {"d_pitch": "41"}),
    ("ロールPを50に", {"p_roll": "50"}),
    ("roll p +3", {"p_roll": "48"}),
])
def test_direct_and_terms(text, expected):
    got, _ = parse(text)
    assert got == expected


def test_out_of_range_explicit_value_is_rejected_by_validation():
    from ai_drone_tune.tuning.apply import validate

    _, r = parse("p_roll=400")
    assert validate(r.changes.changes, _cfg())


def test_relative_change_is_clamped_to_range():
    got, _ = parse("p_roll *9")
    assert got["p_roll"] == "250"


def test_rates_target():
    got, r = parse("最大レート 800")
    assert got == {"roll_srate": "80", "pitch_srate": "80"}
    got, _ = parse("yaw rate 500")
    assert got == {"yaw_srate": "50"}


def test_intents():
    got, _ = parse("プロップウォッシュを減らしたい")
    assert int(got["d_roll"]) > 40 and got["dyn_idle_min_rpm"] == "30"
    got, _ = parse("motors are hot")
    assert int(got["d_roll"]) < 40
    got, _ = parse("もっとキビキビ")
    assert int(got["p_roll"]) > 45 and int(got["f_pitch"]) > 125


def test_unparsed_reported():
    _, r = parse("今日はいい天気")
    assert r.unparsed == ["今日はいい天気"]


def test_multiple_instructions():
    got, _ = parse("p_roll=50; d_roll=42")
    assert got == {"p_roll": "50", "d_roll": "42"}


def test_rate_formulas():
    assert max_rate("ACTUAL", AxisRates(7, 67, 0)) == pytest.approx(670)
    assert rate_at("ACTUAL", AxisRates(7, 67, 0), 0.001) / 0.001 == pytest.approx(70, rel=0.02)
    assert max_rate("BETAFLIGHT", AxisRates(100, 70, 0)) == pytest.approx(200 / 0.3)
    assert max_rate("QUICK", AxisRates(100, 67, 0)) == pytest.approx(670, rel=0.01)
    r = solve_for_max_rate("BETAFLIGHT", AxisRates(100, 70, 0), 800)
    assert abs(max_rate("BETAFLIGHT", r) - 800) < 40

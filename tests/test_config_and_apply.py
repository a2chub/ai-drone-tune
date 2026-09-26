import json

import pytest

from ai_drone_tune.fc.emulator import EmulatedFC
from ai_drone_tune.fc.flight_controller import FlightController
from ai_drone_tune.tuning.apply import ApplyError, apply_changes, build_commands, rollback_changes, validate
from ai_drone_tune.tuning.changes import Change
from ai_drone_tune.tuning.config import from_blackbox_headers, parse_diff_output, parse_get_output

GET_SAMPLE = """p_roll = 45
profile 0
Allowed range: 0 - 250
Default value: 45

gyro_lpf1_type = PT1
Allowed values: PT1, BIQUAD, PT2, PT3
Default value: PT1

roll_srate = 67
rateprofile 1
Allowed range: 0 - 255
Default value: 67

rpm_filter_weights = 100,100,100
Array length: 3
Default value: 100,100,100
"""

DIFF_SAMPLE = """# version
# Betaflight / STM32F7X2 (S7X2) 4.5.1

# master
set gyro_lpf1_static_hz = 300

profile 0
set p_roll = 40

profile 1
set p_roll = 60

rateprofile 0
set roll_srate = 70

# restore original profile selection
profile 1

# restore original rateprofile selection
rateprofile 0

# save configuration
save
"""


def test_parse_get():
    cfg = parse_get_output(GET_SAMPLE)
    assert cfg.values["p_roll"] == "45"
    assert cfg.specs["p_roll"].section == "profile" and cfg.specs["p_roll"].max == 250
    assert cfg.specs["gyro_lpf1_type"].allowed == ["PT1", "BIQUAD", "PT2", "PT3"]
    assert cfg.specs["roll_srate"].section == "rateprofile" and cfg.rate_profile == 1
    assert cfg.specs["rpm_filter_weights"].array_length == 3
    assert cfg.specs["p_roll"].validate("251")
    assert cfg.specs["p_roll"].validate("50") is None
    assert cfg.specs["gyro_lpf1_type"].validate("pt2") is None
    assert cfg.specs["gyro_lpf1_type"].validate("FOO")


def test_parse_diff_selects_active_profile():
    cfg = parse_diff_output(DIFF_SAMPLE)
    assert cfg.pid_profile == 1 and cfg.rate_profile == 0
    assert cfg.values["p_roll"] == "60"
    assert cfg.values["roll_srate"] == "70"
    assert cfg.values["gyro_lpf1_static_hz"] == "300"


def test_headers_mapping():
    cfg = from_blackbox_headers({"Firmware revision": "Betaflight 4.5.1", "rollPID": "45,80,40",
                                 "ff_weight": "120,125,120", "gyro_lpf1_dyn_hz": "250,500",
                                 "rates": "67,67,67", "d_min": "30,32,0", "dyn_notch_count": "3"})
    assert cfg.values["d_roll"] == "40" and cfg.values["f_pitch"] == "125"
    assert cfg.values["gyro_lpf1_dyn_max_hz"] == "500"
    assert cfg.values["d_min_pitch"] == "32"
    assert cfg.section_of("p_roll") == "profile"
    assert cfg.section_of("roll_srate") == "rateprofile"
    assert cfg.section_of("dyn_notch_count") == "master"


def test_build_commands_groups_sections():
    cfg = parse_get_output(GET_SAMPLE)
    cmds = build_commands([Change("roll_srate", "67", "70", "x"), Change("p_roll", "45", "50", "x"),
                           Change("gyro_lpf1_type", "PT1", "PT2", "x")], cfg)
    assert cmds == ["set gyro_lpf1_type = PT2", "profile 0", "set p_roll = 50", "rateprofile 1",
                    "set roll_srate = 70"]


def test_validate_rejects():
    cfg = parse_get_output(GET_SAMPLE)
    errs = validate([Change("p_roll", "45", "300", "x"), Change("nope", None, "1", "x")], cfg)
    assert len(errs) == 2


def _connected(emu):
    fc = FlightController("emu", serial_factory=lambda _d: emu).open()
    fc.identify()
    return fc


def test_apply_verify_save_and_rollback(tmp_path):
    emu = EmulatedFC()
    fc = _connected(emu)
    cfg = parse_get_output(fc.get_all())
    changes = [Change("p_roll", "45", "50", "test", section="profile"),
               Change("dyn_notch_count", "3", "2", "test", section="master"),
               Change("roll_srate", "67", "80", "test", section="rateprofile")]
    res = apply_changes(fc, changes, cfg, backup_dir=tmp_path / "b", history_dir=tmp_path / "h",
                        log=lambda *_: None)
    assert not res.failed and res.saved
    assert emu.saved["p_roll"] == "50" and emu.saved["dyn_notch_count"] == "2" and emu.saved["roll_srate"] == "80"
    assert res.backup_path.exists()
    hist = json.loads(res.history_path.read_text())
    assert len(hist["changes"]) == 3

    back = rollback_changes(res.history_path)
    assert {c.name: c.new for c in back.changes} == {"p_roll": "45", "dyn_notch_count": "3", "roll_srate": "67"}


def test_apply_validation_blocks_everything(tmp_path):
    emu = EmulatedFC()
    fc = _connected(emu)
    cfg = parse_get_output(fc.get_all())
    with pytest.raises(ApplyError):
        apply_changes(fc, [Change("p_roll", "45", "999", "x")], cfg, log=lambda *_: None)
    assert emu.settings["p_roll"][0] == "45"

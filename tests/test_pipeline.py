from ai_drone_tune.fc.emulator import EmulatedFC
from ai_drone_tune.pipeline import UI, run_session
from ai_drone_tune.settings import AppSettings


def _settings(tmp_path, **kw):
    s = AppSettings(home=str(tmp_path), plots=False, auto_min_confidence=0.0)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _ui(answers):
    out = []
    it = iter(answers)
    return UI(out=out.append, ask=lambda _p: next(it)), out


def test_auto_mode_end_to_end(tmp_path, sim_log_bytes):
    emu = EmulatedFC(sim_log_bytes, craft_name="AUTO")
    ui, out = _ui([])
    res = run_session("emu", _settings(tmp_path), "auto", ui, serial_factory=lambda _d: emu)
    assert res.download_path is not None and res.download_path.exists()
    assert len(emu.flash) == 0                         # erased after verified download
    assert res.proposed is not None and res.proposed.changes
    assert res.applied is not None and res.applied.saved
    for c in res.applied.applied:
        assert emu.saved[c.name].upper() == c.new.upper()
    assert any(p.name.endswith(".report.md") for p in res.reports)


def test_approve_mode_rejects_all(tmp_path, sim_log_bytes):
    emu = EmulatedFC(sim_log_bytes)
    ui, out = _ui(["n"] * 50)
    res = run_session("emu", _settings(tmp_path), "approve", ui, serial_factory=lambda _d: emu)
    assert res.approved == []
    assert emu.saved["p_roll"] == "45" and emu.reboots == 1   # CLI exit without save


def test_approve_mode_edit_value(tmp_path, sim_log_bytes):
    emu = EmulatedFC(sim_log_bytes)
    ui, out = _ui(["e", "2", "q"])
    res = run_session("emu", _settings(tmp_path), "approve", ui, serial_factory=lambda _d: emu)
    first = res.proposed.applicable()[0]
    assert [c.new for c in res.approved] == ["2"]
    assert emu.saved[first.name] == "2"


def test_manual_mode(tmp_path, sim_log_bytes):
    emu = EmulatedFC(sim_log_bytes)
    ui, out = _ui(["ロールのPを50に", "最大レート 800", "show", "apply"])
    run_session("emu", _settings(tmp_path), "manual", ui, serial_factory=lambda _d: emu)
    assert emu.saved["p_roll"] == "50"
    assert emu.saved["roll_srate"] == "80" and emu.saved["pitch_srate"] == "80"


def test_empty_flash_auto_mode_does_not_reboot(tmp_path):
    emu = EmulatedFC(b"")
    ui, out = _ui([])
    res = run_session("emu", _settings(tmp_path), "auto", ui, serial_factory=lambda _d: emu)
    assert res.download_path is None and emu.reboots == 0

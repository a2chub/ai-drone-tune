import hashlib
import json
from datetime import datetime

from ai_drone_tune.fc.blackbox_download import build_log_filename, download_blackbox
from ai_drone_tune.fc.emulator import EmulatedFC
from ai_drone_tune.fc.flight_controller import FlightController


def _fc(emu):
    fc = FlightController("emu", serial_factory=lambda _d: emu).open()
    fc.identify()
    return fc


def test_filename():
    name = build_log_filename("My Quad/5\"", "4.5.1", datetime(2026, 9, 26, 14, 3, 5))
    assert name == "My_Quad_5_BF4.5.1_20260926-140305.bbl"


def test_download_verify_erase(tmp_path, sim_log_bytes):
    emu = EmulatedFC(sim_log_bytes, craft_name="RACER")
    fc = _fc(emu)
    res = download_blackbox(fc, tmp_path, verify_second_pass=True, log=lambda *_: None)
    assert res is not None
    assert res.path.parent.name == "RACER"
    assert res.path.name.startswith("RACER_BF4.5.1_") and res.path.suffix == ".bbl"
    assert res.path.read_bytes() == sim_log_bytes
    assert res.sha256 == hashlib.sha256(sim_log_bytes).hexdigest()
    assert res.erased and len(emu.flash) == 0
    meta = json.loads(res.path.with_name(res.path.stem + ".json").read_text())
    assert meta["erased"] is True and meta["fc"]["craft_name"] == "RACER"
    assert res.path.with_name(res.path.stem + ".diff.txt").read_text().startswith("# version")


def test_no_erase_without_valid_log(tmp_path):
    emu = EmulatedFC(b"\x00" * 5000)
    fc = _fc(emu)
    res = download_blackbox(fc, tmp_path, log=lambda *_: None)
    assert res is not None and not res.erased
    assert len(emu.flash) == 5000


def test_no_erase_option(tmp_path, sim_log_bytes):
    emu = EmulatedFC(sim_log_bytes)
    res = download_blackbox(_fc(emu), tmp_path, erase=False, log=lambda *_: None)
    assert not res.erased and len(emu.flash) == len(sim_log_bytes)


def test_empty_flash(tmp_path):
    emu = EmulatedFC(b"")
    assert download_blackbox(_fc(emu), tmp_path, log=lambda *_: None) is None
    assert not emu.cli  # CLI (and therefore a reboot) was not triggered

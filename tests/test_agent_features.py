import json
import subprocess
import sys

from ai_drone_tune.analysis.data_quality import assess, recommend_sample_rate
from ai_drone_tune.blackbox.parser import parse_bytes
from ai_drone_tune.compare import compare, history
from ai_drone_tune.analysis.report import analyze_log
from ai_drone_tune.fc.emulator import EmulatedFC
from ai_drone_tune.journal import append, read, render_markdown
from ai_drone_tune.pipeline import propose_session
from ai_drone_tune.settings import AppSettings
from ai_drone_tune.sim import SimConfig, simulate
from ai_drone_tune.tuning.config import FCConfig
from ai_drone_tune.tuning.flight_plan import build_plan, recommended_fc_settings


def test_quality_good_flight(sim_log_bytes):
    lg = parse_bytes(sim_log_bytes)[0]
    q = assess(lg, None, "tuning", lg.byte_size)
    assert q.metrics["sample_rate_hz"] == 2000
    assert q.metrics["stick_snaps"]["roll"] > 5
    assert not any(i.id.startswith("missing_") for i in q.issues)


def test_quality_short_flight_blocks_and_suggests_segments():
    lg = parse_bytes(simulate(SimConfig(duration_s=5, seed=4)))[0]
    q = assess(lg, None, "tuning")
    assert not q.sufficient
    assert any(i.id == "flight_too_short" and i.severity == "blocker" for i in q.issues)
    assert "roll_snaps" in q.missing_segments()


def test_quality_missing_fields_and_low_rate():
    cfg = SimConfig(duration_s=15, seed=5, log_denom=8)  # 4 kHz loop / 8 = 500 Hz
    data = simulate(cfg)
    lg = parse_bytes(data)[0]
    q = assess(lg, None, "tuning", lg.byte_size)
    ids = {i.id for i in q.issues}
    assert "low_sample_rate" in ids
    assert q.recommended_settings()["blackbox_sample_rate"] == "1/2"


def test_sample_rate_choice():
    assert recommend_sample_rate(8000, "tuning") == "1/4"
    assert recommend_sample_rate(4000, "tuning") == "1/2"
    # race: 16 MB for 6 min at 50 B/frame -> must drop to 500 Hz..1 kHz
    r = recommend_sample_rate(8000, "race", 50, 16_000_000, 360)
    assert r in ("1/8", "1/16")
    assert 8000 / int(r.split("/")[1]) * 50 * 360 * 1.1 <= 16_000_000


def test_plans():
    p = build_plan("tuning")
    ids = [s["id"] for s in p["segments"]]
    assert ids[0] == "mode_acro" and "propwash_chops" in ids and p["planned_airborne_s"] >= 45
    focused = build_plan("tuning", focus=["yaw_snaps"])
    assert [s["id"] for s in focused["segments"]] == ["mode_acro", "hover", "yaw_snaps"]
    race = build_plan("race", pid_hz=8000, flash_total=16_000_000, flash_used=0, race_duration_s=120, heats=2)
    assert race["flash_budget"]["fits_plan"]
    assert any(s["name"] == "blackbox_sample_rate" for s in race["fc_settings"])


def test_fc_settings_against_config():
    cfg = FCConfig(values={"blackbox_disable_gyrounfilt": "ON", "blackbox_disable_setpoint": "OFF",
                           "blackbox_sample_rate": "1/8", "debug_mode": "NONE", "dshot_bidir": "OFF",
                           "craft_name": "X"}, firmware="4.5.1")
    rec = {r["name"]: r for r in recommended_fc_settings(cfg, "tuning", pid_hz=8000)}
    assert rec["blackbox_disable_gyrounfilt"]["recommended"] == "OFF"
    assert "blackbox_disable_setpoint" not in rec
    assert rec["blackbox_sample_rate"]["recommended"] == "1/4"
    assert rec["dshot_bidir"]["advisory"]


def test_journal(tmp_path):
    append(tmp_path, "Q1", "feedback", "プロップウォッシュが気になる")
    append(tmp_path, "Q1", "change", "applied", {"changes": [{"name": "d_roll", "old": "40", "new": "44"}]})
    e = read(tmp_path, "Q1")
    assert [x["kind"] for x in e] == ["feedback", "change"]
    md = render_markdown("Q1", e)
    assert "d_roll" in md and "プロップウォッシュ" in md


def test_compare_detects_change(sim_log_bytes, sim_log_bytes_norpm):
    a = analyze_log(parse_bytes(sim_log_bytes_norpm)[0])
    b = analyze_log(parse_bytes(sim_log_bytes)[0])
    res = compare(a, b)
    row = next(r for r in res["metrics"] if r["metric"] == "roll.filtered_hf_rms")
    assert row["verdict"] == "better"
    assert any(c["name"] == "dshot_bidir" for c in res["setting_changes"])


def test_propose_session_with_emulator(tmp_path, sim_log_bytes):
    s = AppSettings(home=str(tmp_path), plots=False)
    emu = EmulatedFC(sim_log_bytes, craft_name="AGENT")
    out = propose_session(s, "emu", serial_factory=lambda _d: emu, log=lambda *_: None)
    assert out["download"]["erased"] and len(emu.flash) == 0
    assert emu.saved["p_roll"] == "45"                      # propose never writes settings
    assert out["data_quality"]["purpose"] == "tuning"
    assert out["flight_plan"]["purpose"] == "tuning"
    prop = json.loads(open(out["proposal_file"]).read())
    assert "changes" in prop and "data_quality" in prop
    h = history(tmp_path, "AGENT")
    assert h["logs"] and h["proposals"]
    kinds = [e["kind"] for e in read(tmp_path, "AGENT")]
    assert kinds == ["download", "analysis"]
    json.dumps(out, default=str)  # must be serialisable


def test_propose_withholds_pid_when_self_level(tmp_path):
    data = simulate(SimConfig(duration_s=15, seed=6))
    # flip ANGLE mode bit on in every slow frame (flightModeFlags is the first slow field)
    data = data.replace(b"S\x01\x00\x00\x01\x01", b"S\x03\x00\x00\x01\x01")
    path = tmp_path / "angle.bbl"
    path.write_bytes(data)
    s = AppSettings(home=str(tmp_path), plots=False)
    out = propose_session(s, None, path, log=lambda *_: None)
    ids = {i["id"] for i in out["data_quality"]["issues"]}
    assert "self_level_mode" in ids
    cats = {c["category"] for c in out["recommendations"]["changes"]}
    assert "pid" not in cats and "feedforward" not in cats


def test_cli_json_commands(tmp_path, sim_log_bytes):
    f = tmp_path / "x.bbl"
    f.write_bytes(sim_log_bytes)
    run = lambda *a: subprocess.run([sys.executable, "-m", "ai_drone_tune.cli", "--home", str(tmp_path), *a],
                                    capture_output=True, text=True, check=True).stdout
    assert json.loads(run("check", str(f), "--json"))["logs"][0]["sufficient"] in (True, False)
    assert json.loads(run("plan", "--purpose", "race", "--pid-hz", "8000", "--json"))["purpose"] == "race"
    out = json.loads(run("propose", "--file", str(f), "--json", "--no-plots"))
    assert out["proposal_file"]
    assert json.loads(run("compare", str(f), str(f), "--json"))["summary"]["worse"] == 0
    run("journal", "add", "--craft", "SIM", "--kind", "feedback", "ok")
    assert json.loads(run("journal", "show", "--craft", "SIM", "--json"))["entries"][-1]["text"] == "ok"


def test_race_plan_reports_analysis_limit():
    race = build_plan("race", pid_hz=4000, flash_total=16_777_216, flash_used=0, race_duration_s=150, heats=3)
    assert race["flash_budget"]["log_rate_hz"] == 500
    assert any("250Hz" in x for x in race["analysis_limits"])


def test_cli_emulated_set_requires_yes(tmp_path, sim_log_bytes):
    f = tmp_path / "x.bbl"
    f.write_bytes(sim_log_bytes)
    base = [sys.executable, "-m", "ai_drone_tune.cli", "--home", str(tmp_path), "--emulate", str(f)]
    dry = subprocess.run(base + ["set", "p_roll=48", "--dry-run", "--json"], capture_output=True, text=True, check=True)
    assert json.loads(dry.stdout)["changes"][0]["new"] == "48"
    refused = subprocess.run(base + ["set", "p_roll=48", "--json"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert refused.returncode == 1 and "--yes" in refused.stderr
    ok = subprocess.run(base + ["set", "p_roll=48", "--yes", "--json"], capture_output=True, text=True, check=True)
    assert json.loads(ok.stdout)["saved"] is True

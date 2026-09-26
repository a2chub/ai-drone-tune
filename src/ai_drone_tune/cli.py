"""`aidt` command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .settings import MODES, AppSettings, default_config_path


def _settings(args) -> AppSettings:
    s = AppSettings.load(getattr(args, "config", None))
    for attr in ("home", "mode"):
        v = getattr(args, attr, None)
        if v:
            setattr(s, attr, v)
    if getattr(args, "no_erase", False):
        s.erase_after_download = False
    if getattr(args, "verify", False):
        s.verify_second_pass = True
    if getattr(args, "llm", False):
        s.llm = True
    if getattr(args, "no_plots", False):
        s.plots = False
    return s


def _emit(args, obj, text: str | None = None) -> None:
    """Print JSON when --json was given, otherwise the human readable text (or JSON)."""
    if getattr(args, "json", False) or text is None:
        print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    else:
        print(text)


def _log_fn(args):
    return (lambda m: print(m, file=sys.stderr)) if getattr(args, "json", False) else print


def _confirm(args, changes, action: str = "apply") -> bool:
    """Writes to the FC need --yes, or an interactive y/N."""
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        print(f"refusing to {action} without --yes (non-interactive)", file=sys.stderr)
        return False
    for c in changes:
        print(f"  {c.describe()}")
    return input(f"{action} {len(changes)} change(s) to the FC and save? [y/N] ").strip().lower() in ("y", "yes")


_EMULATOR = None


def _serial_factory(args):
    """`--emulate FILE.bbl` swaps the USB port for an in-memory Betaflight emulator."""
    global _EMULATOR
    if not getattr(args, "emulate", None):
        return None
    if _EMULATOR is None:
        from .fc.emulator import EmulatedFC

        _EMULATOR = EmulatedFC(Path(args.emulate).read_bytes(), craft_name="EMU_QUAD")
        _EMULATOR.reboots = 0

    def factory(_device):
        _EMULATOR.closed = False
        _EMULATOR.cli = False
        return _EMULATOR

    return factory


def _pick_port(args, settings: AppSettings) -> str:
    if getattr(args, "emulate", None):
        return "emulator"
    if getattr(args, "port", None):
        return args.port
    from .fc.ports import list_fc_ports

    ports = list_fc_ports(settings.include_generic_uart)
    if not ports:
        sys.exit("No Betaflight flight controller found on USB (use --port to specify one).")
    if len(ports) > 1:
        print("Several flight controllers found, using the first: " + ", ".join(p.device for p in ports))
    return ports[0].device


def _open_fc(args, settings):
    from .fc.flight_controller import FlightController

    fc = FlightController(_pick_port(args, settings), settings.baudrate, serial_factory=_serial_factory(args)).open()
    fc.identify()
    return fc


# ---------------------------------------------------------------------------
def cmd_ports(args) -> None:
    from .fc.ports import list_fc_ports

    ports = list_fc_ports(include_generic=True)
    if not ports:
        print("No flight controller detected.")
    for p in ports:
        print(f"{p.device}\t{p.vid:04x}:{p.pid:04x}\t{p.description}\t{p.serial_number or ''}")


def cmd_info(args) -> None:
    s = _settings(args)
    fc = _open_fc(args, s)
    try:
        print(json.dumps(fc.info.to_dict(), indent=2, ensure_ascii=False))
        fl = fc.msp.dataflash_summary()
        print(f"dataflash: supported={fl.supported} ready={fl.ready} used={fl.used_size}/{fl.total_size} bytes")
    finally:
        fc.close()


def cmd_download(args) -> None:
    from .fc.blackbox_download import download_blackbox
    from .journal import append

    s = _settings(args)
    fc = _open_fc(args, s)
    try:
        res = download_blackbox(fc, s.log_dir, verify_second_pass=s.verify_second_pass,
                                erase=s.erase_after_download, log=_log_fn(args))
        if res:
            append(Path(s.home), fc.info.craft_name, "download",
                   f"downloaded {res.path.name} ({res.log_count} log(s), erased={res.erased})", {"file": str(res.path)})
            _emit(args, {"file": str(res.path), "bytes": res.size, "logs": res.log_count, "erased": res.erased,
                         "sha256": res.sha256, "extra_files": [str(f) for f in res.extra_files]},
                  f"saved: {res.path}  logs={res.log_count}  erased={res.erased}")
        else:
            _emit(args, {"empty": True}, "blackbox flash is empty")
    finally:
        if fc.cli is not None and fc.cli.active:
            fc.exit_cli()
        fc.close()


def cmd_analyze(args) -> None:
    from .analysis.report import analyze_log
    from .blackbox.parser import parse_file
    from .report import markdown_report, save_reports
    from .tuning.config import from_blackbox_headers
    from .tuning.recommender import Thresholds, recommend

    s = _settings(args)
    logs = parse_file(args.file)
    if not logs:
        sys.exit("No blackbox logs found in file.")
    selected = [lg for lg in logs if args.log is None or lg.index + 1 == args.log]
    out_dir = Path(args.out) if args.out else s.report_dir / "offline"
    for lg in selected:
        print(f"== log #{lg.index + 1}: {lg.duration_s:.1f}s, {len(lg.frames)} frames, {lg.firmware_revision}")
        try:
            a = analyze_log(lg)
        except ValueError as e:
            print(f"   skipped: {e}")
            continue
        cfg = from_blackbox_headers(lg.headers)
        cs = recommend(a, cfg, Thresholds(max_step=s.max_step))
        base = out_dir / f"{Path(args.file).stem}_log{lg.index + 1}"
        files = save_reports(a, cs, base, str(args.file), plots=s.plots)
        if args.json:
            print(json.dumps(a.to_dict() | {"recommendations": cs.to_dict()}, indent=2, ensure_ascii=False,
                             default=float))
        else:
            print(markdown_report(a, cs, str(args.file)))
        print("files: " + ", ".join(str(f) for f in files))


def cmd_tune(args) -> None:
    from .pipeline import run_session

    s = _settings(args)
    run_session(_pick_port(args, s), s, args.mode, bbl_file=Path(args.file) if args.file else None,
                serial_factory=_serial_factory(args))


def cmd_watch(args) -> None:
    from .pipeline import watch

    s = _settings(args)
    try:
        watch(s, args.mode, once=args.once)
    except KeyboardInterrupt:
        print("\nstopped")


def _connected_cfg(fc):
    from .tuning.config import parse_get_output

    return parse_get_output(fc.get_all())


def cmd_apply(args) -> None:
    from .tuning.apply import apply_changes
    from .tuning.changes import ChangeSet

    s = _settings(args)
    data = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
    cs = ChangeSet.from_dict(data)
    items = cs.applicable()
    if args.only:
        wanted = set(args.only.split(","))
        items = [c for c in items if c.name in wanted]
    if not items:
        _emit(args, {"applied": [], "message": "nothing to apply"}, "nothing to apply")
        return
    if not _confirm(args, items):
        sys.exit(1)
    fc = _open_fc(args, s)
    try:
        cfg = _connected_cfg(fc)
        for c in items:
            cur = cfg.get(c.name)
            if cur is not None and c.old is not None and str(cur) != str(c.old) and not args.force:
                sys.exit(f"{c.name} is {cur} on the FC but the proposal expected {c.old}; use --force to apply anyway")
            c.old = cur
        res = apply_changes(fc, items, cfg, backup_dir=s.backup_dir, history_dir=s.history_dir, save=not args.dry_run,
                            log=_log_fn(args), analysis_ref=str(args.proposal), journal_home=Path(s.home))
        _emit(args, _apply_result(res))
    finally:
        if fc.port is not None:
            fc.exit_cli()
            fc.close()


def _apply_result(res) -> dict:
    return {"applied": [c.to_dict() for c in res.applied],
            "failed": [{"change": c.to_dict(), "error": e} for c, e in res.failed],
            "saved": res.saved, "backup": str(res.backup_path) if res.backup_path else None,
            "history": str(res.history_path) if res.history_path else None}


def cmd_set(args) -> None:
    from .tuning.apply import apply_changes, validate
    from .tuning.instructions import InstructionParser

    s = _settings(args)
    fc = _open_fc(args, s)
    try:
        cfg = _connected_cfg(fc)
        parsed = InstructionParser(cfg).parse("\n".join(args.assignments))
        changes = parsed.changes.applicable()
        errors = validate(changes, cfg)
        summary = {"changes": [c.to_dict() for c in changes], "unparsed": parsed.unparsed,
                   "messages": parsed.messages, "errors": errors}
        if parsed.unparsed or errors or args.dry_run or not changes:
            summary["applied"] = False
            _emit(args, summary)
            if parsed.unparsed or errors:
                sys.exit(1)
            return
        if not _confirm(args, changes):
            sys.exit(1)
        res = apply_changes(fc, changes, cfg, backup_dir=s.backup_dir, history_dir=s.history_dir,
                            log=_log_fn(args), journal_home=Path(s.home))
        _emit(args, summary | _apply_result(res))
    finally:
        if fc.port is not None:
            fc.exit_cli()
            fc.close()


def cmd_manual(args) -> None:
    from .pipeline import run_session

    s = _settings(args)
    s.erase_after_download = s.erase_after_download and not args.no_download
    run_session(_pick_port(args, s), s, "manual",
                bbl_file=Path(args.file) if args.file else None)


def cmd_get(args) -> None:
    from dataclasses import asdict

    from .tuning.config import parse_get_output

    s = _settings(args)
    fc = _open_fc(args, s)
    try:
        text = fc.cli_session().command(f"get {args.name}")
        cfg = parse_get_output(text)
        _emit(args, {"settings": [asdict(sp) for sp in cfg.specs.values()]}, text)
    finally:
        fc.exit_cli()
        fc.close()


def cmd_backup(args) -> None:
    from datetime import datetime

    from .fc.blackbox_download import sanitize

    s = _settings(args)
    fc = _open_fc(args, s)
    try:
        diff = fc.diff_all()
        d = s.backup_dir / sanitize(fc.info.craft_name)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{sanitize(fc.info.craft_name)}_BF{fc.info.version}_{datetime.now():%Y%m%d-%H%M%S}.diff.txt"
        path.write_text(diff, encoding="utf-8")
        print(f"saved {path}")
    finally:
        fc.exit_cli()
        fc.close()


def cmd_restore(args) -> None:
    s = _settings(args)
    lines = [ln.strip() for ln in Path(args.diff).read_text(encoding="utf-8").splitlines()]
    cmds = [ln for ln in lines if ln and not ln.startswith("#") and ln not in ("save", "batch start", "batch end")]
    if not args.yes and (not sys.stdin.isatty() or
                         input(f"replay {len(cmds)} CLI line(s) from {args.diff} and save? [y/N] ").strip().lower() != "y"):
        sys.exit("aborted (use --yes to confirm)")
    fc = _open_fc(args, s)
    cli = fc.cli_session()
    errors = 0
    for c in cmds:
        try:
            cli.command(c)
        except Exception as e:
            errors += 1
            print(f"  error: {c}: {e}")
    if errors and not args.force:
        print(f"{errors} error(s); not saving (use --force to save anyway). Leaving CLI.")
        fc.exit_cli()
        return
    print(f"restored {len(cmds) - errors} line(s); saving ...")
    fc.save_and_reboot()


def cmd_rollback(args) -> None:
    from .tuning.apply import apply_changes, latest_history, rollback_changes

    s = _settings(args)
    fc = _open_fc(args, s)
    try:
        hist = Path(args.history) if args.history else latest_history(s.history_dir, fc.info.craft_name)
        if hist is None:
            sys.exit("no tuning history for this craft")
        cs = rollback_changes(hist)
        print(f"rolling back {hist.name}:", file=sys.stderr)
        if not _confirm(args, cs.applicable(), "roll back"):
            sys.exit(1)
        cfg = _connected_cfg(fc)
        res = apply_changes(fc, cs.applicable(), cfg, backup_dir=s.backup_dir, history_dir=s.history_dir,
                            log=_log_fn(args), journal_home=Path(s.home))
        _emit(args, _apply_result(res))
    finally:
        if fc.port is not None:
            fc.exit_cli()
            fc.close()


def cmd_status(args) -> None:
    from .compare import history
    from .fc.ports import list_fc_ports
    from .journal import read

    s = _settings(args)
    out: dict = {"home": s.home, "mode": s.mode, "ports": [p.device for p in list_fc_ports(s.include_generic_uart)]}
    craft = args.craft
    if out["ports"] or args.port or args.emulate:
        fc = _open_fc(args, s)
        try:
            out["fc"] = fc.info.to_dict()
            fl = fc.msp.dataflash_summary()
            out["flash"] = {"supported": fl.supported, "ready": fl.ready, "used_bytes": fl.used_size,
                            "total_bytes": fl.total_size,
                            "used_pct": round(fl.used_size * 100 / fl.total_size, 1) if fl.total_size else None}
            try:
                bb = fc.msp.blackbox_config()
                out["blackbox"] = {"device": bb.device, "sample_rate": bb.sample_rate,
                                   "log_rate_hz": round(fc.info.pid_loop_hz / bb.rate_denom) if bb.rate_denom and fc.info.pid_loop_hz else None,
                                   "fields_disabled_mask": bb.fields_disabled_mask}
            except Exception:
                pass
            craft = craft or fc.info.craft_name
        finally:
            fc.close()
    if craft:
        h = history(Path(s.home), craft)
        out["craft"] = craft
        out["latest_log"] = h["logs"][-1] if h["logs"] else None
        out["latest_report"] = h["reports"][-1] if h["reports"] else None
        out["latest_proposal"] = h["proposals"][-1] if h["proposals"] else None
        out["last_applied"] = h["applied_changes"][-1] if h["applied_changes"] else None
        out["journal_tail"] = read(Path(s.home), craft, last=5)
    _emit(args, out)


def cmd_propose(args) -> None:
    from .pipeline import propose_session

    s = _settings(args)
    device = None
    if not args.file or args.port:
        device = _pick_port(args, s)
    out = propose_session(s, device, Path(args.file) if args.file else None, args.purpose,
                          args.race_duration, args.heats, serial_factory=_serial_factory(args))
    if args.json:
        _emit(args, out)
        return
    dq = out.get("data_quality") or {}
    print(f"log: {out.get('log_file')}")
    if dq:
        print(f"data quality ({dq['purpose']}): score {dq['score']}  sufficient={dq['sufficient']}")
        for i in dq["issues"]:
            print(f"  [{i['severity']}] {i['message']}")
    rec = out.get("recommendations") or {}
    for c in rec.get("changes", []):
        print(f"  {c['name']}: {c['old']} -> {c['new']}  ({c['reason']})" + ("  [advisory]" if c.get("advisory") else ""))
    for n in rec.get("notes", []):
        print(f"  note: {n}")
    if out.get("proposal_file"):
        print(f"proposal: {out['proposal_file']}  (apply with: aidt apply <file> --yes)")


def _analyses_for(path: str):
    from .analysis.report import analyze_log
    from .blackbox.parser import parse_file

    return [(lg, analyze_log(lg)) for lg in parse_file(path) if lg.duration_s > 1]


def cmd_check(args) -> None:
    from .analysis.data_quality import assess
    from .blackbox.parser import parse_file

    logs = parse_file(args.file)
    if not logs:
        sys.exit("no blackbox logs in file")
    results = []
    for lg in logs:
        if args.log and lg.index + 1 != args.log:
            continue
        q = assess(lg, None, args.purpose, lg.byte_size, args.flash_bytes,
                   args.race_duration * args.heats if args.purpose == "race" else None)
        results.append({"log": lg.index + 1, **q.to_dict()})
    text = []
    for r in results:
        text.append(f"== log #{r['log']}: score {r['score']} sufficient={r['sufficient']} "
                    f"(airborne {r['metrics'].get('airborne_s')}s, {r['metrics'].get('sample_rate_hz')}Hz)")
        text += [f"  [{i['severity']}] {i['message']}" for i in r["issues"]]
        if r["recommended_settings"]:
            text.append("  recommended settings: " + ", ".join(f"{k}={v}" for k, v in r["recommended_settings"].items()))
        if r["missing_segments"]:
            text.append("  fly: " + ", ".join(r["missing_segments"]))
    _emit(args, {"file": args.file, "logs": results}, "\n".join(text))


def cmd_plan(args) -> None:
    from .analysis.data_quality import assess, pid_loop_hz_from_headers
    from .blackbox.parser import parse_file
    from .tuning.config import from_blackbox_headers
    from .tuning.flight_plan import build_plan

    focus = None
    cfg = None
    pid_hz = None
    if args.from_log:
        logs = parse_file(args.from_log)
        if logs:
            lg = max(logs, key=lambda x: x.duration_s)
            q = assess(lg, None, args.purpose, lg.byte_size)
            focus = q.missing_segments() or None
            cfg = from_blackbox_headers(lg.headers)
            pid_hz = pid_loop_hz_from_headers(lg)
    plan = build_plan(args.purpose, focus=focus, cfg=cfg, pid_hz=pid_hz or args.pid_hz,
                      flash_total=args.flash_bytes, race_duration_s=args.race_duration, heats=args.heats)
    _emit(args, plan, _plan_text(plan))


def _plan_text(plan: dict) -> str:
    lines = [f"# {plan['goal']}", "", "## 準備"] + [f"- {x}" for x in plan["prerequisites"]]
    fm = plan["flight_mode"]
    lines += ["", f"## フライトモード: {fm['mode']} / Air Mode {fm['air_mode']}"] + [f"- {x}" for x in fm["notes"]]
    if plan.get("fc_settings"):
        lines += ["", "## 推奨 FC 設定"]
        for st in plan["fc_settings"]:
            adv = " (要確認・手動)" if st.get("advisory") else ""
            lines.append(f"- `{st['name']}`: {st['current']} → **{st['recommended']}**{adv} - {st['reason']}")
    lines += ["", f"## 飛行手順 (合計 約 {plan['planned_airborne_s']} 秒)"]
    for i, sg in enumerate(plan["segments"], 1):
        rep = f" x{sg['repeat']}" if sg["repeat"] > 1 else ""
        lines.append(f"{i}. **{sg['title']}**{rep} ({sg['duration_s']:.0f}s) - スロットル: {sg['throttle']} / "
                     f"操作: {sg['sticks']}  ※{sg['why']}")
    if plan.get("flash_budget"):
        fb = plan["flash_budget"]
        lines += ["", "## Flash 容量", f"- {json.dumps(fb, ensure_ascii=False)}"]
    if plan.get("analysis_limits"):
        lines += ["", "## 注意"] + [f"- {x}" for x in plan["analysis_limits"]]
    lines += ["", "## 飛行後"] + [f"- {x}" for x in plan["after_flight"]]
    return "\n".join(lines)


def cmd_preflight(args) -> None:
    from .tuning.flight_plan import build_plan

    s = _settings(args)
    fc = _open_fc(args, s)
    try:
        info = fc.info
        fl = fc.msp.dataflash_summary()
        cfg = _connected_cfg(fc)
        cfg.firmware = info.version
    finally:
        if fc.port is not None:
            fc.exit_cli()
            fc.close()
    plan = build_plan(args.purpose, cfg=cfg, pid_hz=info.pid_loop_hz or None, flash_total=fl.total_size or None,
                      flash_used=fl.used_size, race_duration_s=args.race_duration, heats=args.heats)
    checks = []
    if fl.used_size:
        checks.append({"id": "flash_not_empty", "message": f"Blackbox Flash に {fl.used_size} bytes 残っています。"
                       " 飛行前に aidt download (ダウンロード後に消去) を実行してください。"})
    if info.armed:
        checks.append({"id": "armed", "message": "FC がアーム状態です。"})
    out = {"fc": info.to_dict(), "checks": checks, "plan": plan,
           "apply_hint": "aidt set " + " ".join(f"{x['name']}={x['recommended']}" for x in plan["fc_settings"]
                                                if not x.get("advisory")) + " --yes"}
    _emit(args, out, "\n".join([f"- {c['message']}" for c in checks] + ["", _plan_text(plan), "",
                                                                         "設定を反映するには: " + out["apply_hint"]]))


def cmd_compare(args) -> None:
    from .compare import compare, compare_markdown

    a = _analyses_for(args.before)
    b = _analyses_for(args.after)
    pick = (lambda lst, n: next(x for x in lst if x[0].index + 1 == n) if n else max(lst, key=lambda x: x[1].data.flight_time_s))
    if not a or not b:
        sys.exit("both files need at least one analysable log")
    ra, rb = pick(a, args.log_before)[1], pick(b, args.log_after)[1]
    res = compare(ra, rb)
    _emit(args, res, compare_markdown(res, args.before, args.after))


def cmd_history(args) -> None:
    from .compare import history
    from .journal import crafts

    s = _settings(args)
    if not args.craft:
        _emit(args, {"crafts": crafts(Path(s.home))})
        return
    _emit(args, history(Path(s.home), args.craft))


def cmd_journal(args) -> None:
    from .journal import append, read, render_markdown

    s = _settings(args)
    action, text = args.action[0], args.action[1:]
    if action not in ("add", "show"):
        sys.exit("journal action must be `add` or `show`")
    if action == "add":
        if not text:
            sys.exit("journal add needs text")
        e = append(Path(s.home), args.craft, args.kind, " ".join(text))
        _emit(args, e, f"added: {e['time']} [{e['kind']}] {e['text']}")
    else:
        entries = read(Path(s.home), args.craft, last=args.last)
        _emit(args, {"craft": args.craft, "entries": entries}, render_markdown(args.craft, entries))


def cmd_rates(args) -> None:
    from .tuning.rates import AxisRates, describe, rate_at, solve_for_max_rate

    r = AxisRates(args.rc_rate, args.srate, args.expo)
    if args.target:
        r = solve_for_max_rate(args.type, r, args.target)
    print(f"{args.type}: {describe(args.type, r)}")
    for x in (0.0, 0.25, 0.5, 0.75, 1.0):
        print(f"  stick {x:4.2f} -> {rate_at(args.type, r, x):7.1f} deg/s")


def cmd_simulate(args) -> None:
    from .sim import SimConfig, simulate

    cfg = SimConfig(duration_s=args.duration, seed=args.seed, rpm_filter=not args.no_rpm)
    if args.p:
        cfg.p = tuple(args.p)
    if args.d:
        cfg.d = tuple(args.d)
    Path(args.out).write_bytes(simulate(cfg))
    print(f"wrote {args.out}")


def cmd_demo(args) -> None:
    """Full pipeline against an emulated FC loaded with a simulated flight."""
    import tempfile

    from .fc.emulator import DEFAULT_SETTINGS, EmulatedFC
    from .pipeline import UI, run_session
    from .sim import SimConfig, simulate

    s = _settings(args)
    if not args.home:
        s.home = tempfile.mkdtemp(prefix="aidt-demo-")
    print(f"demo output directory: {s.home}")
    emu = EmulatedFC(simulate(SimConfig(duration_s=args.duration, seed=args.seed)), craft_name="DEMO_QUAD")
    ui = UI()
    if (args.mode or s.mode) == "approve" and args.yes:
        ui = UI(ask=lambda prompt: (print(prompt + "y"), "y")[1])
    run_session("emulator", s, args.mode, ui, serial_factory=lambda _dev: emu)
    defaults = {n: v for n, v, _s, _r in DEFAULT_SETTINGS}
    defaults["craft_name"] = "DEMO_QUAD"
    changed = {k: f"{defaults.get(k)} -> {v}" for k, v in emu.saved.items() if defaults.get(k) != v}
    print("settings saved on the emulated FC that differ from defaults:", changed or "none")


def cmd_msc(args) -> None:
    from .fc.msp import REBOOT_MSC

    s = _settings(args)
    fc = _open_fc(args, s)
    fc.msp.reboot(REBOOT_MSC)
    fc.close()
    print("FC rebooting into mass-storage mode; copy logs with `aidt import <mount point>`.")


def cmd_import(args) -> None:
    from .fc.blackbox_download import import_from_directory

    s = _settings(args)
    for p in import_from_directory(Path(args.source), s.log_dir):
        print(f"imported {p}")


def cmd_config(args) -> None:
    s = AppSettings.load(args.config)
    if args.set:
        for item in args.set:
            k, v = item.split("=", 1)
            if not hasattr(s, k):
                sys.exit(f"unknown setting {k}")
            cur = getattr(s, k)
            if isinstance(cur, bool):
                v = v.lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, float):
                v = float(v)
            elif isinstance(cur, int):
                v = int(v)
            elif isinstance(cur, list):
                v = [x.strip() for x in v.split(",") if x.strip()]
            setattr(s, k, v)
        print(f"saved {s.save(args.config)}")
    from dataclasses import asdict

    print(json.dumps(asdict(s), indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aidt", description="Betaflight blackbox auto-tuner")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", type=Path, help=f"settings file (default {default_config_path()})")
    p.add_argument("--home", help="data directory for logs / reports / backups")
    p.add_argument("--emulate", metavar="BBL", help="use an emulated FC with this blackbox content (no hardware)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def port_arg(sp):
        sp.add_argument("--port", help="serial port (default: auto-detect)")

    sp = sub.add_parser("watch", help="wait for USB connection and run download/analyse/tune automatically")
    sp.add_argument("--mode", choices=MODES)
    sp.add_argument("--no-erase", action="store_true", help="keep blackbox data on the FC after download")
    sp.add_argument("--verify", action="store_true", help="read flash twice and compare before erasing")
    sp.add_argument("--llm", action="store_true", help="use Claude for free-form manual instructions")
    sp.add_argument("--once", action="store_true", help="exit after the first session")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("tune", help="one session on the connected FC")
    port_arg(sp)
    sp.add_argument("--mode", choices=MODES)
    sp.add_argument("--file", help="analyse this log instead of downloading")
    sp.add_argument("--no-erase", action="store_true")
    sp.add_argument("--verify", action="store_true")
    sp.add_argument("--llm", action="store_true")
    sp.add_argument("--no-plots", action="store_true")
    sp.set_defaults(func=cmd_tune)

    sp = sub.add_parser("manual", help="interactive manual tuning (instructions in JP/EN or CLI syntax)")
    port_arg(sp)
    sp.add_argument("--file", help="log to use as analysis context")
    sp.add_argument("--no-download", action="store_true")
    sp.add_argument("--llm", action="store_true")
    sp.set_defaults(func=cmd_manual)

    def purpose_args(sp):
        sp.add_argument("--purpose", choices=["tuning", "race"], default="tuning")
        sp.add_argument("--race-duration", type=float, default=180.0, help="seconds per race heat")
        sp.add_argument("--heats", type=int, default=1)

    sp = sub.add_parser("status", help="connected FC, flash usage and latest tuning state (JSON)")
    port_arg(sp)
    sp.add_argument("--craft")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("propose", help="download + analyse + data check + recommendations, never writes settings")
    port_arg(sp)
    sp.add_argument("--file", help="use this log (no FC needed unless --port is given)")
    purpose_args(sp)
    sp.add_argument("--no-erase", action="store_true")
    sp.add_argument("--verify", action="store_true")
    sp.add_argument("--no-plots", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_propose)

    sp = sub.add_parser("check", help="is this log sufficient for analysis? what to change / fly")
    sp.add_argument("file")
    sp.add_argument("--log", type=int)
    purpose_args(sp)
    sp.add_argument("--flash-bytes", type=int)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("plan", help="flight plan + recommended FC settings for an analysis flight or race recording")
    purpose_args(sp)
    sp.add_argument("--from-log", help="focus the plan on what this log is missing")
    sp.add_argument("--pid-hz", type=float, help="PID loop rate (Hz) if no log/FC is available")
    sp.add_argument("--flash-bytes", type=int)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("preflight", help="check the connected FC's blackbox setup and produce the flight plan")
    port_arg(sp)
    purpose_args(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_preflight)

    sp = sub.add_parser("compare", help="compare two flights (before/after a change)")
    sp.add_argument("before")
    sp.add_argument("after")
    sp.add_argument("--log-before", type=int)
    sp.add_argument("--log-after", type=int)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("history", help="logs / applied changes / reports of a craft")
    sp.add_argument("--craft")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("journal", help="tuning journal per craft")
    sp.add_argument("action", nargs="+", metavar="{add TEXT...,show}",
                    help="`add <text>` appends an entry, `show` prints the journal")
    sp.add_argument("--craft", required=True)
    sp.add_argument("--kind", default="note", choices=["flight", "feedback", "note", "plan", "race", "change",
                                                      "download", "analysis", "proposal"])
    sp.add_argument("--last", type=int)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_journal)

    sp = sub.add_parser("ports", help="list detected flight controllers")
    sp.set_defaults(func=cmd_ports)
    sp = sub.add_parser("info", help="show FC identification and flash usage")
    port_arg(sp)
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("download", help="download blackbox (and erase after verified download)")
    port_arg(sp)
    sp.add_argument("--no-erase", action="store_true")
    sp.add_argument("--verify", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser("analyze", help="analyse a .bbl/.bfl file offline")
    sp.add_argument("file")
    sp.add_argument("--log", type=int, help="log number inside the file (1-based)")
    sp.add_argument("--out", help="report directory")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--no-plots", action="store_true")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("apply", help="apply a saved .proposal.json")
    port_arg(sp)
    sp.add_argument("proposal")
    sp.add_argument("--only", help="comma separated setting names")
    sp.add_argument("--force", action="store_true", help="apply even if FC values changed since the proposal")
    sp.add_argument("--dry-run", action="store_true", help="set but do not save (FC reboots without saving)")
    sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("set", help="set values / instructions directly: aidt set p_roll=50 'd_pitch +5%%'")
    port_arg(sp)
    sp.add_argument("assignments", nargs="+")
    sp.add_argument("--dry-run", action="store_true", help="only show the resulting changes")
    sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_set)

    sp = sub.add_parser("get", help="print a CLI setting (substring match)")
    port_arg(sp)
    sp.add_argument("name")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_get)

    sp = sub.add_parser("backup", help="save `diff all`")
    port_arg(sp)
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("restore", help="replay a `diff all` file and save")
    port_arg(sp)
    sp.add_argument("diff")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("rollback", help="undo the last applied tuning step")
    port_arg(sp)
    sp.add_argument("--history", help="history json to roll back (default: latest)")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_rollback)

    sp = sub.add_parser("rates", help="rate curve calculator")
    sp.add_argument("--type", default="ACTUAL", choices=["BETAFLIGHT", "RACEFLIGHT", "KISS", "ACTUAL", "QUICK"])
    sp.add_argument("--rc-rate", type=int, default=7)
    sp.add_argument("--srate", type=int, default=67)
    sp.add_argument("--expo", type=int, default=0)
    sp.add_argument("--target", type=float, help="solve for this max rate (deg/s)")
    sp.set_defaults(func=cmd_rates)

    sp = sub.add_parser("simulate", help="write a synthetic blackbox log")
    sp.add_argument("out")
    sp.add_argument("--duration", type=float, default=30)
    sp.add_argument("--seed", type=int, default=1)
    sp.add_argument("--p", type=int, nargs=3)
    sp.add_argument("--d", type=int, nargs=3)
    sp.add_argument("--no-rpm", action="store_true")
    sp.set_defaults(func=cmd_simulate)

    sp = sub.add_parser("demo", help="run the whole pipeline against an emulated FC")
    sp.add_argument("--mode", choices=MODES, default="approve")
    sp.add_argument("--yes", action="store_true", help="approve everything (approve mode)")
    sp.add_argument("--duration", type=float, default=30)
    sp.add_argument("--seed", type=int, default=1)
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("msc", help="reboot an SD-card FC into USB mass-storage mode")
    port_arg(sp)
    sp.set_defaults(func=cmd_msc)

    sp = sub.add_parser("import", help="import logs from a mounted SD card / folder")
    sp.add_argument("source")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("config", help="show / change persistent settings")
    sp.add_argument("--set", nargs="+", metavar="KEY=VALUE")
    sp.set_defaults(func=cmd_config)
    return p


def main(argv=None) -> None:
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if extra:
        if args.cmd == "journal" and not any(x.startswith("-") for x in extra):
            args.action += extra  # free text after the options: `journal add --craft X some text`
        else:
            parser.error("unrecognized arguments: " + " ".join(extra))
    args.func(args)


if __name__ == "__main__":
    main()

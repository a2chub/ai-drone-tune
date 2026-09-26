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


def _pick_port(args, settings: AppSettings) -> str:
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

    fc = FlightController(_pick_port(args, settings), settings.baudrate).open()
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

    s = _settings(args)
    fc = _open_fc(args, s)
    try:
        res = download_blackbox(fc, s.log_dir, verify_second_pass=s.verify_second_pass,
                                erase=s.erase_after_download)
        if res:
            print(f"saved: {res.path}  logs={res.log_count}  erased={res.erased}")
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
    run_session(_pick_port(args, s), s, args.mode, bbl_file=Path(args.file) if args.file else None)


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
    fc = _open_fc(args, s)
    try:
        cfg = _connected_cfg(fc)
        for c in items:
            cur = cfg.get(c.name)
            if cur is not None and c.old is not None and str(cur) != str(c.old) and not args.force:
                sys.exit(f"{c.name} is {cur} on the FC but the proposal expected {c.old}; use --force to apply anyway")
            c.old = cur
        apply_changes(fc, items, cfg, backup_dir=s.backup_dir, history_dir=s.history_dir, save=not args.dry_run)
    finally:
        if fc.port is not None:
            fc.exit_cli()
            fc.close()


def cmd_set(args) -> None:
    from .tuning.apply import apply_changes
    from .tuning.instructions import InstructionParser

    s = _settings(args)
    fc = _open_fc(args, s)
    try:
        cfg = _connected_cfg(fc)
        parsed = InstructionParser(cfg).parse("\n".join(args.assignments))
        if parsed.unparsed:
            sys.exit(f"not understood: {parsed.unparsed}")
        for m in parsed.messages:
            print(m)
        apply_changes(fc, parsed.changes.applicable(), cfg, backup_dir=s.backup_dir, history_dir=s.history_dir)
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
    s = _settings(args)
    fc = _open_fc(args, s)
    try:
        print(fc.cli_session().command(f"get {args.name}"))
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
        print(f"rolling back {hist.name}:")
        for c in cs.changes:
            print(f"  {c.describe()}")
        cfg = _connected_cfg(fc)
        apply_changes(fc, cs.applicable(), cfg, backup_dir=s.backup_dir, history_dir=s.history_dir)
    finally:
        if fc.port is not None:
            fc.exit_cli()
            fc.close()


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

    sp = sub.add_parser("ports", help="list detected flight controllers")
    sp.set_defaults(func=cmd_ports)
    sp = sub.add_parser("info", help="show FC identification and flash usage")
    port_arg(sp)
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("download", help="download blackbox (and erase after verified download)")
    port_arg(sp)
    sp.add_argument("--no-erase", action="store_true")
    sp.add_argument("--verify", action="store_true")
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
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("set", help="set values / instructions directly: aidt set p_roll=50 'd_pitch +5%%'")
    port_arg(sp)
    sp.add_argument("assignments", nargs="+")
    sp.set_defaults(func=cmd_set)

    sp = sub.add_parser("get", help="print a CLI setting (substring match)")
    port_arg(sp)
    sp.add_argument("name")
    sp.set_defaults(func=cmd_get)

    sp = sub.add_parser("backup", help="save `diff all`")
    port_arg(sp)
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("restore", help="replay a `diff all` file and save")
    port_arg(sp)
    sp.add_argument("diff")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("rollback", help="undo the last applied tuning step")
    port_arg(sp)
    sp.add_argument("--history", help="history json to roll back (default: latest)")
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
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

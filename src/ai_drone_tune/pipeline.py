"""End-to-end session: connect -> download -> erase -> analyse -> tune (auto / approve / manual)."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .analysis.report import LogAnalysis, analyze_log, pick_best
from .blackbox.parser import parse_file
from .fc.blackbox_download import DownloadError, download_blackbox, sanitize
from .fc.flight_controller import FlightController
from .report import save_reports
from .settings import AppSettings
from .tuning.apply import ApplyResult, apply_changes, validate
from .tuning.changes import Change, ChangeSet
from .tuning.config import FCConfig, from_blackbox_headers, merge, parse_get_output
from .tuning.instructions import InstructionParser, llm_parse
from .tuning.recommender import Thresholds, recommend

# settings compared between the flight log and the FC to detect a stale log
STALE_CHECK_KEYS = ("p_roll", "d_roll", "p_pitch", "d_pitch", "f_roll", "gyro_lpf1_dyn_min_hz",
                    "gyro_lpf2_static_hz", "dterm_lpf1_dyn_min_hz", "dyn_notch_count")


@dataclass
class UI:
    """Console I/O hooks (replaceable for tests / GUIs)."""

    out: Callable[[str], None] = print
    ask: Callable[[str], str] = input


@dataclass
class SessionResult:
    download_path: Path | None = None
    analysis: LogAnalysis | None = None
    proposed: ChangeSet | None = None
    approved: list[Change] = field(default_factory=list)
    applied: ApplyResult | None = None
    reports: list[Path] = field(default_factory=list)


def analyze_file(path: Path, ui: UI = UI()) -> tuple[list[LogAnalysis], LogAnalysis | None]:
    logs = parse_file(path)
    analyses = []
    for lg in logs:
        try:
            analyses.append(analyze_log(lg))
        except ValueError as e:
            ui.out(f"  log #{lg.index + 1}: skipped ({e})")
    return analyses, pick_best(analyses)


def stale_settings(log_cfg: FCConfig, fc_cfg: FCConfig) -> list[str]:
    diffs = []
    for k in STALE_CHECK_KEYS:
        a, b = log_cfg.get(k), fc_cfg.get(k)
        if a is None or b is None:
            continue
        try:
            if int(float(a)) != int(float(b)):
                diffs.append(f"{k}: log={a} fc={b}")
        except ValueError:
            continue
    return diffs


def show_changes(cs: ChangeSet, ui: UI) -> None:
    if not cs.changes:
        ui.out("  (no changes)")
    for i, c in enumerate(cs.changes, 1):
        ui.out(f"  {i:2d}. [{c.category}] {c.describe()}  conf={c.confidence:.2f}")
    for n in cs.notes:
        ui.out(f"  note: {n}")


def approval_gate(cs: ChangeSet, cfg: FCConfig, ui: UI) -> list[Change]:
    """Interactive per-change approval. Returns approved changes."""
    approved: list[Change] = []
    items = cs.applicable()
    advisory = [c for c in cs.changes if c.advisory]
    for c in advisory:
        ui.out(f"  advisory (not applied automatically): {c.describe()}")
    if not items:
        return approved
    ui.out("Approve each change: [y]es / [n]o / [e]dit value / [a]ll remaining / [q]uit (discard rest)")
    accept_all = False
    for idx, c in enumerate(items, 1):
        if accept_all:
            approved.append(c)
            continue
        while True:
            ans = ui.ask(f"  ({idx}/{len(items)}) {c.describe()} ? ").strip().lower()
            if ans in ("y", "yes", ""):
                approved.append(c)
                break
            if ans in ("n", "no"):
                break
            if ans in ("a", "all"):
                approved.append(c)
                accept_all = True
                break
            if ans in ("q", "quit"):
                return approved
            if ans.startswith("e"):
                new = ui.ask(f"    new value for {c.name} (current {c.old}): ").strip()
                if new:
                    c2 = Change(c.name, c.old, new, c.reason + " (edited)", c.category, 1.0, c.section)
                    err = validate([c2], cfg)
                    if err:
                        ui.out(f"    {err[0]}")
                        continue
                    approved.append(c2)
                break
    return approved


def manual_loop(cfg: FCConfig, ui: UI, settings: AppSettings, analysis: LogAnalysis | None = None) -> list[Change]:
    """Collect instructions until the user types `apply` / `done` / `quit`."""
    pending = ChangeSet()
    ui.out("Manual mode. Examples: `p_roll=50`, `ロールのDを5%上げて`, `最大レート 800`, `propwash`, "
           "`show`, `undo NAME`, `apply`, `quit`")
    summary = analysis.to_dict() if analysis else None
    while True:
        text = ui.ask("manual> ").strip()
        if not text:
            continue
        low = text.lower()
        if low in ("quit", "exit", "q", "cancel", "やめる"):
            return []
        if low in ("apply", "done", "save", "適用", "保存"):
            return pending.applicable()
        if low in ("show", "list", "確認"):
            show_changes(pending, ui)
            continue
        if low.startswith("undo "):
            name = low.split()[1]
            pending.changes = [c for c in pending.changes if c.name != name]
            continue
        if low.startswith("get "):
            name = low.split()[1]
            ui.out(f"  {name} = {cfg.get(name)}")
            continue
        # evaluate on top of pending changes
        work = FCConfig(values=dict(cfg.values), specs=cfg.specs, sections=cfg.sections,
                        pid_profile=cfg.pid_profile, rate_profile=cfg.rate_profile)
        for c in pending.changes:
            work.values[c.name] = c.new
        parsed = InstructionParser(work).parse(text)
        new_cs = parsed.changes
        for m in parsed.messages:
            ui.out(f"  {m}")
        if parsed.unparsed and settings.llm:
            try:
                llm_cs, expl = llm_parse(" / ".join(parsed.unparsed), work, summary, settings.llm_model)
                ui.out(f"  AI: {expl}")
                for c in llm_cs.changes:
                    new_cs.add(c)
                parsed.unparsed = []
            except Exception as e:  # network / key problems must not kill the session
                ui.out(f"  AI assistant unavailable: {e}")
        if parsed.unparsed:
            ui.out(f"  not understood: {parsed.unparsed}")
        errors = validate(new_cs.changes, cfg)
        for e in errors:
            ui.out(f"  invalid: {e}")
        bad = {e.split(":")[0] for e in errors}
        for c in new_cs.changes:
            if c.name in bad:
                continue
            base_old = cfg.get(c.name)
            pending.add(Change(c.name, base_old, c.new, c.reason, c.category, c.confidence, c.section))
        show_changes(new_cs, ui)


def run_session(device: str, settings: AppSettings, mode: str | None = None, ui: UI = UI(),
                bbl_file: Path | None = None, serial_factory=None) -> SessionResult:
    mode = mode or settings.mode
    res = SessionResult()
    fc = FlightController(device, settings.baudrate, serial_factory=serial_factory).open()
    try:
        info = fc.identify()
        ui.out(f"Connected: {info.craft_name or '(no craft name)'}  Betaflight {info.version}  "
               f"board {info.board}  API {info.api_version}  on {device}")

        if bbl_file is None:
            try:
                dl = download_blackbox(fc, settings.log_dir, verify_second_pass=settings.verify_second_pass,
                                       erase=settings.erase_after_download, log=ui.out,
                                       progress=_progress_printer(ui))
            except DownloadError as e:
                ui.out(f"Blackbox download skipped: {e}")
                dl = None
            if dl is not None:
                res.download_path = dl.path
                ui.out(f"Saved {dl.path} ({dl.log_count} log(s))")
            elif mode != "manual":
                ui.out("No new flight data - nothing to tune.")
                return res
            bbl_file = res.download_path

        # current FC configuration (all settings with ranges)
        ui.out("Reading FC configuration ...")
        fc_cfg = parse_get_output(fc.get_all())
        fc_cfg.firmware = info.version

        best = None
        if bbl_file is not None:
            ui.out(f"Analysing {bbl_file} ...")
            analyses, best = analyze_file(Path(bbl_file), ui)
            if best is None:
                ui.out("No log with enough flight time (>= 3 s) found.")
        cfg = fc_cfg
        if best is not None:
            log_cfg = from_blackbox_headers(best.log.headers)
            cfg = merge(fc_cfg, log_cfg)
            res.analysis = best
            stale = stale_settings(log_cfg, fc_cfg)
            th = Thresholds(max_step=settings.max_step)
            cs = recommend(best, cfg, th)
            cs.changes = [c for c in cs.changes if c.category in settings.categories or c.advisory]
            if stale:
                cs.notes.append("FC settings differ from those in the flight log (" + "; ".join(stale) +
                                ") - recommendations were computed against the FC's current values")
            res.proposed = cs
            base = settings.report_dir / sanitize(info.craft_name) / Path(bbl_file).stem
            res.reports = save_reports(best, cs, base, str(bbl_file), plots=settings.plots)
            ui.out(f"Report: {res.reports[0]}")
            ui.out("Proposed changes:")
            show_changes(cs, ui)
            proposal = base.parent / (base.name + ".proposal.json")
            res.reports.append(proposal)
            proposal.write_text(json.dumps({"fc": info.to_dict(), "pid_profile": cfg.pid_profile,
                                            "rate_profile": cfg.rate_profile, **cs.to_dict()},
                                           indent=2, ensure_ascii=False), encoding="utf-8")

            if mode == "auto":
                if stale:
                    ui.out("Auto mode: FC configuration changed since this flight - not applying automatically. "
                           "Fly again and reconnect, or use approve mode.")
                else:
                    res.approved = [c for c in cs.applicable() if c.confidence >= settings.auto_min_confidence]
                    skipped = len(cs.applicable()) - len(res.approved)
                    if skipped:
                        ui.out(f"Auto mode: {skipped} low-confidence change(s) skipped "
                               f"(< {settings.auto_min_confidence}).")
            elif mode == "approve":
                if ui.ask is input and not sys.stdin.isatty():
                    ui.out("No terminal for the approval gate (running as a service): proposal saved - "
                           "review the report and run `aidt apply <proposal.json>`.")
                else:
                    res.approved = approval_gate(cs, cfg, ui)

        if mode == "manual":
            if ui.ask is input and not sys.stdin.isatty():
                ui.out("Manual mode needs a terminal - skipped.")
            else:
                res.approved = manual_loop(cfg, ui, settings, best)

        if res.approved:
            res.applied = apply_changes(fc, res.approved, cfg, backup_dir=settings.backup_dir,
                                        history_dir=settings.history_dir, log=ui.out,
                                        analysis_ref=str(res.reports[0]) if res.reports else None)
            ui.out(f"Applied {len(res.applied.applied)} change(s)"
                   + (f", {len(res.applied.failed)} failed" if res.applied.failed else "")
                   + (" and saved." if res.applied.saved else "."))
        else:
            ui.out("Nothing applied.")
        return res
    finally:
        if fc.port is not None:
            if fc.cli is not None and fc.cli.active:
                fc.exit_cli()  # leaves CLI (FC reboots, nothing saved)
            fc.close()


def _progress_printer(ui: UI):
    state = {"last": -1.0}

    def cb(done: int, total: int) -> None:
        pct = done * 100.0 / max(total, 1)
        if pct - state["last"] >= 10 or done == total:
            state["last"] = pct
            ui.out(f"  {pct:5.1f}%  {done}/{total} bytes")

    return cb


def watch(settings: AppSettings, mode: str | None = None, ui: UI = UI(), poll_s: float = 1.0,
          rearm_s: float = 8.0, once: bool = False) -> None:
    """Wait for a flight controller to be plugged in and run a session for it.

    After a session the FC usually reboots (save / CLI exit) and re-enumerates;
    the same device is ignored until it has been unplugged for ``rearm_s``.
    """
    from .fc.ports import list_fc_ports

    handled: dict[str, float] = {}  # key -> time last seen
    ui.out("Waiting for a Betaflight flight controller on USB ... (Ctrl+C to stop)")
    while True:
        now = time.monotonic()
        ports = list_fc_ports(settings.include_generic_uart)
        present = set()
        for p in ports:
            key = p.serial_number or p.device
            present.add(key)
            if key in handled:
                handled[key] = now
                continue
            time.sleep(1.0)  # let the VCP settle after enumeration
            try:
                run_session(p.device, settings, mode, ui)
            except Exception as e:  # keep watching even if one session fails
                ui.out(f"Session failed: {e}")
            handled[key] = time.monotonic()
            if once:
                return
            ui.out("Done. Unplug the quad (or plug in another one).")
        for key in list(handled):
            if key not in present and now - handled[key] > rearm_s:
                del handled[key]
        time.sleep(poll_s)

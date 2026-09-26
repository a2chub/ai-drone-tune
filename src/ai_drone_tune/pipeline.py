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
from . import journal
from .analysis.data_quality import assess
from .report import save_reports
from .settings import AppSettings
from .tuning.apply import ApplyResult, apply_changes, validate
from .tuning.changes import Change, ChangeSet
from .tuning.config import FCConfig, from_blackbox_headers, merge, parse_get_output
from .tuning.instructions import InstructionParser, llm_parse
from .tuning.flight_plan import build_plan
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
                journal.append(Path(settings.home), info.craft_name, "download",
                               f"downloaded {dl.path.name} ({dl.log_count} log(s), erased={dl.erased})",
                               {"file": str(dl.path)})
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
                                        journal_home=Path(settings.home),
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


# ---------------------------------------------------------------------------
# Non-interactive flow for scripts / LLM agents
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def propose_session(settings: AppSettings, device: str | None = None, bbl_file: Path | None = None,
                    purpose: str = "tuning", race_duration_s: float = 180.0, heats: int = 1,
                    serial_factory=None, log: Callable[[str], None] = _log) -> dict:
    """Download (if connected), analyse, assess data quality, recommend - never writes settings.

    Returns a JSON-friendly summary; the proposal / report files are written to disk.
    """
    out: dict = {"purpose": purpose, "fc": None, "download": None, "log_file": None, "analysis": None,
                 "data_quality": None, "recommendations": None, "flight_plan": None, "files": []}
    fc_cfg = None
    info = None
    flash_total = flash_used = None
    pid_hz = None
    if device is not None:
        fc = FlightController(device, settings.baudrate, serial_factory=serial_factory).open()
        try:
            info = fc.identify()
            out["fc"] = info.to_dict()
            pid_hz = info.pid_loop_hz or None
            try:
                fl = fc.msp.dataflash_summary()
                flash_total, flash_used = fl.total_size or None, fl.used_size
            except Exception:
                pass
            if bbl_file is None:
                try:
                    dl = download_blackbox(fc, settings.log_dir, verify_second_pass=settings.verify_second_pass,
                                           erase=settings.erase_after_download, log=log)
                except DownloadError as e:
                    out["download"] = {"error": str(e)}
                    dl = None
                if dl is not None:
                    bbl_file = dl.path
                    out["download"] = {"file": str(dl.path), "bytes": dl.size, "logs": dl.log_count,
                                       "erased": dl.erased, "sha256": dl.sha256}
                    if dl.erased:
                        flash_used = 0
                    journal.append(Path(settings.home), info.craft_name, "download",
                                   f"downloaded {dl.path.name} ({dl.log_count} log(s), erased={dl.erased})",
                                   {"file": str(dl.path)})
                elif out["download"] is None:
                    out["download"] = {"empty": True}
            fc_cfg = parse_get_output(fc.get_all())
            fc_cfg.firmware = info.version
        finally:
            if fc.port is not None:
                if fc.cli is not None and fc.cli.active:
                    fc.exit_cli()
                fc.close()

    craft = info.craft_name if info else ""
    best = None
    quality = None
    if bbl_file is not None:
        out["log_file"] = str(bbl_file)
        analyses, best = analyze_file(Path(bbl_file), UI(out=log))
        target = best
        if target is None and analyses:
            target = max(analyses, key=lambda a: a.data.flight_time_s)
        if target is not None:
            craft = craft or target.log.craft_name
            from .analysis.data_quality import pid_loop_hz_from_headers

            pid_hz = pid_hz or pid_loop_hz_from_headers(target.log)
            planned = race_duration_s * heats if purpose == "race" else None
            quality = assess(target.log, target.data, purpose, target.log.byte_size, flash_total, planned)
            out["data_quality"] = quality.to_dict()
        else:
            from .blackbox.parser import parse_file as _pf

            logs = _pf(Path(bbl_file))
            if logs:
                quality = assess(max(logs, key=lambda lg: lg.duration_s), None, purpose)
                out["data_quality"] = quality.to_dict()

    cfg = fc_cfg
    if best is not None:
        log_cfg = from_blackbox_headers(best.log.headers)
        cfg = merge(fc_cfg, log_cfg) if fc_cfg is not None else log_cfg
        cs = recommend(best, cfg, Thresholds(max_step=settings.max_step))
        cs.changes = [c for c in cs.changes if c.category in settings.categories or c.advisory]
        if fc_cfg is not None:
            stale = stale_settings(log_cfg, fc_cfg)
            if stale:
                cs.notes.append("FC settings differ from the flight log: " + "; ".join(stale))
        if quality is not None and not quality.sufficient:
            blockers = [i.id for i in quality.issues if i.severity == "blocker"]
            if blockers:
                dropped = [c for c in cs.changes if c.category in ("pid", "feedforward")]
                cs.changes = [c for c in cs.changes if c.category not in ("pid", "feedforward")]
                if dropped:
                    cs.notes.append(f"PID/FF recommendations withheld - data insufficient ({', '.join(blockers)})")
        out["analysis"] = best.to_dict()
        out["recommendations"] = cs.to_dict()
        base = settings.report_dir / sanitize(craft) / Path(bbl_file).stem
        files = save_reports(best, cs, base, str(bbl_file), plots=settings.plots)
        proposal = base.parent / (base.name + ".proposal.json")
        proposal.write_text(json.dumps({"fc": out["fc"], "pid_profile": cfg.pid_profile,
                                        "rate_profile": cfg.rate_profile, "log_file": str(bbl_file),
                                        "data_quality": out["data_quality"], **cs.to_dict()},
                                       indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        out["files"] = [str(f) for f in files] + [str(proposal)]
        out["proposal_file"] = str(proposal)
        journal.append(Path(settings.home), craft or "unnamed", "analysis",
                       f"analysed {Path(bbl_file).name}: {len(cs.applicable())} change(s) proposed, "
                       f"data quality {quality.score if quality else '-'}",
                       {"proposal": str(proposal), "report": str(files[0])})

    focus = quality.missing_segments() if quality is not None and quality.issues else None
    out["flight_plan"] = build_plan(purpose, focus=focus, cfg=cfg, pid_hz=pid_hz, flash_total=flash_total,
                                    flash_used=flash_used, race_duration_s=race_duration_s, heats=heats,
                                    bytes_per_frame=(best.log.byte_size / len(best.log.frames)
                                                     if best is not None and len(best.log.frames) else 50.0))
    return out

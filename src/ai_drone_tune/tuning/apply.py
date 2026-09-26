"""Validate, apply, verify, save and roll back setting changes over the CLI."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..fc.cli_session import CLIError
from .changes import Change, ChangeSet
from .config import MASTER, PROFILE, RATEPROFILE, FCConfig


class ApplyError(Exception):
    pass


@dataclass
class ApplyResult:
    applied: list[Change] = field(default_factory=list)
    failed: list[tuple[Change, str]] = field(default_factory=list)
    backup_path: Path | None = None
    history_path: Path | None = None
    saved: bool = False


def validate(changes: list[Change], cfg: FCConfig) -> list[str]:
    errors = []
    for c in changes:
        spec = cfg.specs.get(c.name)
        if cfg.specs and spec is None:
            errors.append(f"{c.name}: unknown setting on this firmware")
            continue
        if spec is not None:
            err = spec.validate(c.new)
            if err:
                errors.append(err)
    return errors


def build_commands(changes: list[Change], cfg: FCConfig) -> list[str]:
    """CLI commands, grouped by section with the right profile selected."""
    groups: dict[str, list[Change]] = {MASTER: [], PROFILE: [], RATEPROFILE: []}
    for c in changes:
        section = c.section or cfg.section_of(c.name)
        groups.setdefault(section, []).append(c)
    cmds: list[str] = []
    for c in groups.get(MASTER, []):
        cmds.append(f"set {c.name} = {c.new}")
    if groups.get(PROFILE):
        cmds.append(f"profile {cfg.pid_profile}")
        cmds.extend(f"set {c.name} = {c.new}" for c in groups[PROFILE])
    if groups.get(RATEPROFILE):
        cmds.append(f"rateprofile {cfg.rate_profile}")
        cmds.extend(f"set {c.name} = {c.new}" for c in groups[RATEPROFILE])
    for section, items in groups.items():
        if section not in (MASTER, PROFILE, RATEPROFILE):
            cmds.extend(f"set {c.name} = {c.new}" for c in items)
    return cmds


def _parse_get_value(text: str, name: str) -> str | None:
    m = re.search(rf"^{re.escape(name)}\s*=\s*(.*)$", text.replace("\r", ""), flags=re.M)
    return m.group(1).strip() if m else None


def _same(a: str | None, b: str) -> bool:
    if a is None:
        return False
    a, b = a.strip(), str(b).strip()
    if a.upper() == b.upper():
        return True
    try:
        return abs(float(a) - float(b)) < 1e-6
    except ValueError:
        return False


def apply_changes(fc, changes: list[Change], cfg: FCConfig, *, backup_dir: Path | None = None,
                  history_dir: Path | None = None, save: bool = True, log=print,
                  analysis_ref: str | None = None) -> ApplyResult:
    """Apply ``changes`` through the FC CLI. ``fc`` is an opened FlightController."""
    result = ApplyResult()
    changes = [c for c in changes if not c.advisory]
    if not changes:
        log("No changes to apply.")
        return result
    errors = validate(changes, cfg)
    if errors:
        raise ApplyError("validation failed:\n  " + "\n  ".join(errors))

    cli = fc.cli_session()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    craft = re.sub(r"[^\w.\-]+", "_", fc.info.craft_name or "unnamed")
    if backup_dir is not None:
        backup_dir = Path(backup_dir) / craft
        backup_dir.mkdir(parents=True, exist_ok=True)
        diff = cli.command("diff all", timeout=10)
        result.backup_path = backup_dir / f"{craft}_{stamp}_before.diff.txt"
        result.backup_path.write_text(diff, encoding="utf-8")
        log(f"Backup of current configuration: {result.backup_path}")

    section_cmd = None
    for cmd in build_commands(changes, cfg):
        if cmd.startswith(("profile ", "rateprofile ")):
            cli.command(cmd)
            section_cmd = cmd
            continue
        name = cmd.split()[1]
        change = next(c for c in changes if c.name == name)
        try:
            out = cli.command(cmd)
            if "Invalid" in out or "ERROR" in out:
                raise CLIError(out.strip())
            got = _parse_get_value(cli.command(f"get {name}"), name)
            if not _same(got, change.new):
                raise CLIError(f"read back {got!r}, expected {change.new!r}")
            result.applied.append(change)
            log(f"  set {name} = {change.new}" + (f"   [{section_cmd}]" if change.section != MASTER and section_cmd else ""))
        except CLIError as e:
            result.failed.append((change, str(e)))
            log(f"  FAILED {name}: {e}")

    if history_dir is not None and result.applied:
        hdir = Path(history_dir) / craft
        hdir.mkdir(parents=True, exist_ok=True)
        result.history_path = hdir / f"{stamp}.json"
        result.history_path.write_text(json.dumps({
            "timestamp": stamp,
            "fc": fc.info.to_dict(),
            "pid_profile": cfg.pid_profile,
            "rate_profile": cfg.rate_profile,
            "analysis": analysis_ref,
            "backup": str(result.backup_path) if result.backup_path else None,
            "changes": [c.to_dict() for c in result.applied],
            "failed": [{"change": c.to_dict(), "error": e} for c, e in result.failed],
        }, indent=2, ensure_ascii=False), encoding="utf-8")

    if result.failed and result.applied:
        log("Some changes failed; saving the ones that succeeded.")
    if save and result.applied:
        log("Saving configuration (FC will reboot) ...")
        fc.save_and_reboot()
        result.saved = True
    return result


def rollback_changes(history_file: Path) -> ChangeSet:
    """Inverse change set for a history entry."""
    data = json.loads(Path(history_file).read_text(encoding="utf-8"))
    cs = ChangeSet()
    for d in data.get("changes", []):
        c = Change.from_dict(d)
        if c.old is None:
            continue
        cs.add(Change(c.name, c.new, c.old, f"rollback of {data.get('timestamp')}", c.category, 1.0, c.section))
    return cs


def latest_history(history_dir: Path, craft: str) -> Path | None:
    d = Path(history_dir) / re.sub(r"[^\w.\-]+", "_", craft or "unnamed")
    files = sorted(d.glob("*.json"))
    return files[-1] if files else None

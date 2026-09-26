"""Per-craft tuning journal: what was flown, changed and felt - across sessions.

Stored as JSON lines (``journal/<craft>.jsonl``) and rendered as Markdown on
demand, so both people and an LLM can pick up the history of a craft.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .fc.blackbox_download import sanitize

KINDS = ("flight", "download", "analysis", "proposal", "change", "feedback", "note", "plan", "race")


def journal_path(home: Path, craft: str) -> Path:
    return Path(home) / "journal" / f"{sanitize(craft)}.jsonl"


def append(home: Path, craft: str, kind: str, text: str, data: dict | None = None,
           when: datetime | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    entry = {"time": (when or datetime.now()).isoformat(timespec="seconds"), "kind": kind, "text": text}
    if data:
        entry["data"] = data
    p = journal_path(home, craft)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return entry


def read(home: Path, craft: str, last: int | None = None) -> list[dict]:
    p = journal_path(home, craft)
    if not p.exists():
        return []
    entries = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    return entries[-last:] if last else entries


def crafts(home: Path) -> list[str]:
    d = Path(home) / "journal"
    return sorted(p.stem for p in d.glob("*.jsonl")) if d.exists() else []


def render_markdown(craft: str, entries: list[dict]) -> str:
    lines = [f"# Tuning journal - {craft}", ""]
    for e in entries:
        lines.append(f"- **{e['time'].replace('T', ' ')}** `[{e['kind']}]` {e['text']}")
        ch = (e.get("data") or {}).get("changes")
        if ch:
            for c in ch:
                lines.append(f"    - `{c['name']}`: {c.get('old')} → {c.get('new')}")
    return "\n".join(lines) + "\n"

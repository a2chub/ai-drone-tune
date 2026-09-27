"""Proposed setting changes."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class Change:
    name: str
    old: str | None
    new: str
    reason: str
    category: str = "other"        # filter / pid / feedforward / rates / other
    confidence: float = 0.5        # 0..1
    section: str | None = None     # master / profile / rateprofile (filled from config)
    advisory: bool = False         # hardware-dependent: never applied automatically

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Change":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    def describe(self) -> str:
        arrow = f"{self.old} -> {self.new}" if self.old is not None else f"= {self.new}"
        flag = " [advisory]" if self.advisory else ""
        return f"{self.name}: {arrow}  ({self.reason}){flag}"


@dataclass
class ChangeSet:
    changes: list[Change] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, change: Change) -> None:
        # later rules can refine an earlier proposal for the same setting
        for i, c in enumerate(self.changes):
            if c.name == change.name:
                change.old = c.old
                if change.reason != c.reason:
                    change.reason = f"{c.reason}; {change.reason}"
                self.changes[i] = change
                return
        if change.old is not None and str(change.old) == str(change.new):
            return
        self.changes.append(change)

    def get(self, name: str) -> Change | None:
        return next((c for c in self.changes if c.name == name), None)

    def applicable(self) -> list[Change]:
        return [c for c in self.changes if not c.advisory and str(c.old) != str(c.new)]

    def __iter__(self):
        return iter(self.changes)

    def __len__(self):
        return len(self.changes)

    def to_dict(self) -> dict:
        return {"changes": [c.to_dict() for c in self.changes], "notes": self.notes}

    @classmethod
    def from_dict(cls, d: dict) -> "ChangeSet":
        return cls([Change.from_dict(c) for c in d.get("changes", [])], list(d.get("notes", [])))

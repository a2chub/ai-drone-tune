"""Application settings (JSON file + command line overrides)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

MODES = ("auto", "approve", "manual")


def default_home() -> Path:
    return Path(os.environ.get("AIDT_HOME", Path.home() / "ai-drone-tune"))


def default_config_path() -> Path:
    return Path(os.environ.get("AIDT_CONFIG", Path.home() / ".config" / "ai-drone-tune" / "config.json"))


@dataclass
class AppSettings:
    home: str = field(default_factory=lambda: str(default_home()))
    mode: str = "approve"
    erase_after_download: bool = True
    verify_second_pass: bool = False
    auto_min_confidence: float = 0.5
    max_step: float = 0.15
    plots: bool = True
    llm: bool = False
    llm_model: str = "claude-opus-5"
    baudrate: int = 115200
    include_generic_uart: bool = False
    categories: list[str] = field(default_factory=lambda: ["filter", "pid", "feedforward", "other"])

    @property
    def log_dir(self) -> Path:
        return Path(self.home) / "logs"

    @property
    def report_dir(self) -> Path:
        return Path(self.home) / "reports"

    @property
    def backup_dir(self) -> Path:
        return Path(self.home) / "backups"

    @property
    def history_dir(self) -> Path:
        return Path(self.home) / "history"

    @classmethod
    def load(cls, path: Path | None = None) -> "AppSettings":
        path = Path(path) if path else default_config_path()
        s = cls()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in known:
                    setattr(s, k, v)
        if s.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        return s

    def save(self, path: Path | None = None) -> Path:
        path = Path(path) if path else default_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        return path

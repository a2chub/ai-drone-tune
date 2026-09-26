"""Compare two flights (before / after a tuning step) and list craft history."""

from __future__ import annotations

import json
from pathlib import Path

from .analysis.report import LogAnalysis
from .fc.blackbox_download import sanitize
from .tuning.config import from_blackbox_headers

# metric -> True when a lower value is better
METRICS = {
    "filtered_hf_rms": True, "raw_hf_rms": True, "dterm_hf_pct": True, "filter_delay_ms": True,
    "overshoot_pct": True, "delay_ms": True, "rise_ms": True, "settle_ms": True, "tracking_lag_ms": True,
    "propwash_ratio": True, "high_throttle_osc_ratio": True, "motor_saturation_pct": True,
}


def _metrics(a: LogAnalysis) -> dict:
    out: dict = {}
    for ax in a.noise.axes:
        for k in ("filtered_hf_rms", "raw_hf_rms", "dterm_hf_pct", "filter_delay_ms"):
            out[f"{ax.axis}.{k}"] = getattr(ax, k)
    for s in a.steps:
        for k in ("overshoot_pct", "delay_ms", "rise_ms", "settle_ms", "tracking_lag_ms"):
            out[f"{s.axis}.{k}"] = getattr(s, k)
    b = a.behaviour
    out["propwash_ratio"] = b.propwash_ratio
    out["high_throttle_osc_ratio"] = b.high_throttle_osc_ratio
    out["motor_saturation_pct"] = b.motor_saturation_pct
    return out


def compare(before: LogAnalysis, after: LogAnalysis, rel_threshold: float = 0.05) -> dict:
    mb, ma = _metrics(before), _metrics(after)
    rows = []
    for key in sorted(set(mb) | set(ma)):
        b, a = mb.get(key), ma.get(key)
        base = key.split(".")[-1]
        row = {"metric": key, "before": _r(b), "after": _r(a), "change": None, "verdict": "n/a"}
        if b is not None and a is not None:
            delta = a - b
            row["change"] = _r(delta)
            rel = abs(delta) / max(abs(b), 1e-6)
            if rel < rel_threshold:
                row["verdict"] = "same"
            else:
                lower_better = METRICS.get(base, True)
                row["verdict"] = "better" if (delta < 0) == lower_better else "worse"
        rows.append(row)
    cb = from_blackbox_headers(before.log.headers).values
    ca = from_blackbox_headers(after.log.headers).values
    setting_changes = [{"name": k, "before": cb.get(k), "after": ca.get(k)}
                       for k in sorted(set(cb) | set(ca)) if cb.get(k) != ca.get(k) and not k.startswith("_")]
    counts = {v: sum(1 for r in rows if r["verdict"] == v) for v in ("better", "worse", "same")}
    return {"before": {"log": before.log.index + 1, "flight_s": round(before.data.flight_time_s, 1)},
            "after": {"log": after.log.index + 1, "flight_s": round(after.data.flight_time_s, 1)},
            "summary": counts, "metrics": rows, "setting_changes": setting_changes}


def _r(v):
    return None if v is None else round(float(v), 2)


def compare_markdown(result: dict, name_before: str, name_after: str) -> str:
    lines = ["# Flight comparison", "", f"- before: `{name_before}` ({result['before']['flight_s']}s)",
             f"- after: `{name_after}` ({result['after']['flight_s']}s)",
             f"- better {result['summary']['better']} / worse {result['summary']['worse']} / "
             f"same {result['summary']['same']}", "", "| metric | before | after | change | |", "|---|---|---|---|---|"]
    mark = {"better": "✅", "worse": "⚠️", "same": "・", "n/a": ""}
    for r in result["metrics"]:
        lines.append(f"| {r['metric']} | {r['before']} | {r['after']} | {r['change']} | {mark[r['verdict']]} |")
    if result["setting_changes"]:
        lines += ["", "## Settings that differ between the two logs", ""]
        lines += [f"- `{c['name']}`: {c['before']} → {c['after']}" for c in result["setting_changes"]]
    return "\n".join(lines) + "\n"


def history(home: Path, craft: str) -> dict:
    """Logs, applied change sets and reports for one craft, newest last."""
    c = sanitize(craft)
    home = Path(home)
    logs = []
    for p in sorted((home / "logs" / c).glob("*.bbl")):
        meta = p.with_name(p.stem + ".json")
        m = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
        logs.append({"file": str(p), "size": p.stat().st_size, "downloaded_at": m.get("downloaded_at"),
                     "log_count": m.get("log_count"), "erased": m.get("erased")})
    changes = []
    for p in sorted((home / "history" / c).glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        changes.append({"file": str(p), "timestamp": d.get("timestamp"),
                        "changes": [{k: x.get(k) for k in ("name", "old", "new", "reason")} for x in d.get("changes", [])],
                        "failed": len(d.get("failed", []))})
    reports = [str(p) for p in sorted((home / "reports" / c).glob("*.report.md"))]
    proposals = [str(p) for p in sorted((home / "reports" / c).glob("*.proposal.json"))]
    return {"craft": craft, "logs": logs, "applied_changes": changes, "reports": reports, "proposals": proposals}

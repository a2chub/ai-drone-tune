"""Markdown / JSON reports and optional PNG plots."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .analysis.report import LogAnalysis
from .tuning.changes import ChangeSet


def _v(x, fmt="{:.1f}", none="-"):
    return none if x is None else fmt.format(x)


def markdown_report(a: LogAnalysis, changes: ChangeSet | None, source: str = "") -> str:
    lines = []
    lg = a.log
    lines.append(f"# Tuning report - {lg.craft_name or 'unnamed'}")
    lines.append("")
    lines.append(f"- log: `{source}` (#{lg.index + 1})")
    lines.append(f"- firmware: {lg.firmware_revision}")
    lines.append(f"- duration {lg.duration_s:.1f}s, in flight {a.data.flight_time_s:.1f}s, "
                 f"sample rate {a.data.fs:.0f} Hz")
    for w in a.warnings + a.noise.notes:
        lines.append(f"- warning: {w}")
    lines.append("")
    lines.append("## Noise")
    lines.append("")
    lines.append("| axis | raw >100Hz | filtered >100Hz | attenuation | filter delay | D-term >80Hz |")
    lines.append("|---|---|---|---|---|---|")
    for ax in a.noise.axes:
        lines.append(f"| {ax.axis} | {_v(ax.raw_hf_rms, '{:.2f}')} deg/s | {_v(ax.filtered_hf_rms, '{:.2f}')} deg/s | "
                     f"{_v(ax.attenuation_db)} dB | {_v(ax.filter_delay_ms, '{:.2f}')} ms | "
                     f"{_v(ax.dterm_hf_pct)} % |")
    lines.append("")
    if a.noise.motor:
        m = a.noise.motor
        lines.append(f"Motor fundamental: p10 {m.fundamental_hz_p10:.0f} Hz / median {m.fundamental_hz_p50:.0f} Hz / "
                     f"p90 {m.fundamental_hz_p90:.0f} Hz; filter attenuation at the motor frequency (raw -> filtered): "
                     f"{_v(m.rpm_filter_attenuation_db)} dB")
        lines.append("")
    peaks = {}
    for ax in a.noise.axes[:2]:
        for p in ax.raw_peaks + ax.filtered_peaks:
            peaks.setdefault(round(p.freq / 10) * 10, p)
    if peaks:
        lines.append("Spectral peaks: " + ", ".join(f"{p.freq:.0f} Hz ({p.kind})" for _, p in sorted(peaks.items())))
        lines.append("")
    lines.append("## Step response")
    lines.append("")
    lines.append("| axis | overshoot | delay (50%) | rise 10-90% | settle | steady | setpoint lag | windows |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in a.steps:
        lines.append(f"| {s.axis} | {s.overshoot_pct:.1f} % | {s.delay_ms:.1f} ms | {s.rise_ms:.1f} ms | "
                     f"{s.settle_ms:.0f} ms | {s.steady:.2f} | {_v(s.tracking_lag_ms)} ms | {s.windows_used} |")
    lines.append("")
    b = a.behaviour
    lines.append("## Flight behaviour")
    lines.append("")
    lines.append(f"- motor saturation: {_v(b.motor_saturation_pct)} % of flight")
    lines.append(f"- propwash error ratio (after throttle chops vs. elsewhere): {_v(b.propwash_ratio, '{:.2f}')}")
    lines.append(f"- high/mid throttle oscillation ratio: {_v(b.high_throttle_osc_ratio, '{:.2f}')}")
    lines.append(f"- throttle mean {b.throttle_mean * 100:.0f} %, p90 {b.throttle_p90 * 100:.0f} %")
    lines.append("")
    if changes is not None:
        lines.append("## Recommended changes")
        lines.append("")
        if not changes.changes:
            lines.append("No changes recommended - the tune looks balanced for this flight.")
        else:
            lines.append("| setting | current | proposed | confidence | reason |")
            lines.append("|---|---|---|---|---|")
            for c in changes.changes:
                flag = " (advisory - manual)" if c.advisory else ""
                lines.append(f"| `{c.name}` | {c.old} | **{c.new}**{flag} | {c.confidence:.2f} | {c.reason} |")
        for n in changes.notes:
            lines.append(f"\n> {n}")
        lines.append("")
    return "\n".join(lines)


def save_reports(a: LogAnalysis, changes: ChangeSet | None, out_base: Path, source: str = "",
                 plots: bool = True) -> list[Path]:
    out_base = Path(out_base)
    out_base.parent.mkdir(parents=True, exist_ok=True)
    md = out_base.parent / (out_base.name + ".report.md")
    md.write_text(markdown_report(a, changes, source), encoding="utf-8")
    js = out_base.parent / (out_base.name + ".analysis.json")
    payload = a.to_dict()
    if changes is not None:
        payload["recommendations"] = changes.to_dict()
    js.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=float), encoding="utf-8")
    files = [md, js]
    if plots:
        png = plot_analysis(a, out_base.parent / (out_base.name + ".png"))
        if png:
            files.append(png)
    return files


def plot_analysis(a: LogAnalysis, path: Path) -> Path | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    sp = a.noise.spectra
    fig, axes = plt.subplots(2, 3, figsize=(16, 8), constrained_layout=True)
    f = sp["freqs"]
    for k, name in enumerate(("roll", "pitch", "yaw")):
        ax = axes[0, k]
        if sp["raw"]:
            ax.plot(f, 10 * np.log10(np.maximum(sp["raw"][k], 1e-12)), label="gyro (unfiltered)", lw=0.8)
        ax.plot(f, 10 * np.log10(np.maximum(sp["filtered"][k], 1e-12)), label="gyro (filtered)", lw=0.8)
        if k < len(sp["dterm"]):
            ax.plot(f, 10 * np.log10(np.maximum(sp["dterm"][k], 1e-12)) - 20, label="D-term (-20 dB)", lw=0.6,
                    alpha=0.7)
        for p in a.noise.resonances:
            ax.axvline(p.freq, color="red", ls=":", lw=0.8)
        ax.set_title(f"{name} noise spectrum")
        ax.set_xlabel("Hz")
        ax.set_ylabel("dB")
        ax.set_xlim(0, f[-1])
        ax.legend(fontsize=7)
        ax2 = axes[1, k]
        s = a.step(name)
        if s is not None:
            ax2.plot(s.t_ms, s.response / (s.steady or 1), lw=1.5)
            ax2.axhline(1.0, color="grey", ls="--", lw=0.8)
            ax2.set_title(f"{name} step response - overshoot {s.overshoot_pct:.0f}%, delay {s.delay_ms:.0f} ms")
        else:
            ax2.set_title(f"{name} step response - not enough stick input")
        ax2.set_xlabel("ms")
        ax2.set_xlim(0, 500)
        ax2.set_ylim(0, 1.6)
    fig.suptitle(f"{a.log.craft_name or ''} {a.log.firmware_revision}")
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path

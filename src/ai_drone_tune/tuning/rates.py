"""Betaflight rate curves (fc/rc.c) and helpers to hit a target max rate."""

from __future__ import annotations

from dataclasses import dataclass

RATES_TYPES = ["BETAFLIGHT", "RACEFLIGHT", "KISS", "ACTUAL", "QUICK"]
RC_RATE_INCREMENTAL = 14.54
AXES = ("roll", "pitch", "yaw")


@dataclass
class AxisRates:
    rc_rate: int   # <axis>_rc_rate
    srate: int     # <axis>_srate
    expo: int      # <axis>_expo
    rate_limit: int = 1998


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def rate_at(rates_type: str, r: AxisRates, stick: float, quick_rates_rc_expo: bool = False) -> float:
    """Angular rate in deg/s for stick deflection ``stick`` in [-1, 1]."""
    rates_type = rates_type.upper()
    x = _clamp(stick, -1.0, 1.0)
    ax = abs(x)
    if rates_type == "BETAFLIGHT":
        if r.expo:
            e = r.expo / 100.0
            x = x * ax ** 3 * e + x * (1 - e)
        rc = r.rc_rate / 100.0
        if rc > 2.0:
            rc += RC_RATE_INCREMENTAL * (rc - 2.0)
        angle = 200.0 * rc * x
        if r.srate:
            angle *= 1.0 / _clamp(1.0 - ax * r.srate / 100.0, 0.01, 1.0)
    elif rates_type == "RACEFLIGHT":
        x = (1.0 + 0.01 * r.expo * (x * x - 1.0)) * x
        angle = 10.0 * r.rc_rate * x
        angle *= 1 + ax * r.srate * 0.01
    elif rates_type == "KISS":
        curve = r.expo / 100.0
        use_rates = 1.0 / _clamp(1.0 - ax * r.srate / 100.0, 0.01, 1.0)
        kx = (x ** 3 * curve + x * (1 - curve)) * (r.rc_rate / 1000.0)
        angle = _clamp(2000.0 * use_rates * kx, -1998, 1998)
    elif rates_type == "ACTUAL":
        e = r.expo / 100.0
        expof = ax * (x ** 5 * e + x * (1 - e))
        center = r.rc_rate * 10.0
        stick_move = max(0.0, r.srate * 10.0 - center)
        angle = x * center + stick_move * expof
    elif rates_type == "QUICK":
        rc = r.rc_rate * 2
        max_dps = max(r.srate * 10, rc)
        e = r.expo / 100.0
        sf_cfg = (max_dps / rc - 1) / (max_dps / rc) if rc else 0.0
        if quick_rates_rc_expo:
            curve = x ** 3 * e + x * (1 - e)
            sf = 1.0 / _clamp(1.0 - ax * sf_cfg, 0.01, 1.0)
            angle = curve * rc * sf
        else:
            curve = ax ** 3 * e + ax * (1 - e)
            sf = 1.0 / _clamp(1.0 - curve * sf_cfg, 0.01, 1.0)
            angle = x * rc * sf
        angle = _clamp(angle, -1998, 1998)
    else:
        raise ValueError(f"unknown rates_type {rates_type}")
    return _clamp(angle, -r.rate_limit, r.rate_limit)


def max_rate(rates_type: str, r: AxisRates) -> float:
    return rate_at(rates_type, r, 1.0)


def center_sensitivity(rates_type: str, r: AxisRates) -> float:
    """deg/s per unit stick near center (slope at 0)."""
    h = 1e-3
    return rate_at(rates_type, r, h) / h


def solve_for_max_rate(rates_type: str, r: AxisRates, target: float) -> AxisRates:
    """Adjust the 'super rate' style parameter so the curve peaks at ``target`` deg/s.

    For ACTUAL / QUICK the max rate parameter is explicit. For BETAFLIGHT /
    KISS / RACEFLIGHT the super rate (srate) is searched while rc_rate
    (center feel) stays unchanged.
    """
    rates_type = rates_type.upper()
    target = _clamp(target, 0, 1998)
    new = AxisRates(r.rc_rate, r.srate, r.expo, r.rate_limit)
    if rates_type in ("ACTUAL", "QUICK"):
        new.srate = int(round(target / 10.0))
        return new
    best = None
    for s in range(0, 101):
        new.srate = s
        err = abs(max_rate(rates_type, new) - target)
        if best is None or err < best[0]:
            best = (err, s)
    new.srate = best[1]
    return new


def describe(rates_type: str, r: AxisRates) -> str:
    return (f"max {max_rate(rates_type, r):.0f} deg/s, center {center_sensitivity(rates_type, r):.0f} deg/s/stick "
            f"(rc_rate={r.rc_rate}, srate={r.srate}, expo={r.expo})")

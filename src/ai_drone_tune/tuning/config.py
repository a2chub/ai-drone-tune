"""Flight controller configuration model.

Sources, in order of preference:

* CLI ``get`` output: every setting with current value, section
  (profile / rateprofile), allowed range / values and default. This makes the
  whole Betaflight CLI surface available without a hard-coded table.
* CLI ``diff all`` / ``dump all`` output (values only).
* Blackbox log headers (offline analysis; values only, subset of settings).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MASTER, PROFILE, RATEPROFILE, BATTERY = "master", "profile", "rateprofile", "battery_profile"


@dataclass
class SettingSpec:
    name: str
    value: str
    section: str = MASTER
    min: float | None = None
    max: float | None = None
    allowed: list[str] | None = None
    array_length: int | None = None
    default: str | None = None
    is_string: bool = False

    def validate(self, value) -> str | None:
        """Return an error message, or None if ``value`` is acceptable."""
        v = str(value).strip()
        if self.allowed is not None:
            if v.upper() not in (a.upper() for a in self.allowed):
                return f"{self.name}: '{v}' not in allowed values {self.allowed}"
            return None
        if self.array_length is not None:
            parts = [p for p in v.split(",") if p.strip() != ""]
            if len(parts) != self.array_length:
                return f"{self.name}: expected {self.array_length} comma separated values"
            return None
        if self.min is not None and self.max is not None:
            try:
                num = float(v)
            except ValueError:
                return f"{self.name}: '{v}' is not a number"
            if not (self.min <= num <= self.max):
                return f"{self.name}: {v} outside allowed range {self.min:g} - {self.max:g}"
        return None


@dataclass
class FCConfig:
    values: dict[str, str] = field(default_factory=dict)          # active profile values
    specs: dict[str, SettingSpec] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)        # name -> section
    pid_profile: int = 0
    rate_profile: int = 0
    source: str = "unknown"
    firmware: str = ""

    def has(self, name: str) -> bool:
        return name in self.values

    def get(self, name: str, default=None):
        return self.values.get(name, default)

    def get_int(self, name: str, default: int | None = None) -> int | None:
        v = self.values.get(name)
        if v is None:
            return default
        try:
            return int(float(v))
        except ValueError:
            spec = self.specs.get(name)
            if v.upper() in ("ON", "TRUE"):
                return 1
            if v.upper() in ("OFF", "FALSE"):
                return 0
            if spec and spec.allowed:
                uppers = [a.upper() for a in spec.allowed]
                if v.upper() in uppers:
                    return uppers.index(v.upper())
            return default

    def is_on(self, name: str) -> bool:
        v = self.values.get(name)
        if v is None:
            return False
        return v.upper() in ("ON", "1", "TRUE") or (v.isdigit() and int(v) > 0)

    def section_of(self, name: str) -> str:
        if name in self.specs:
            return self.specs[name].section
        if name in self.sections:
            return self.sections[name]
        return guess_section(name)

    def clamp(self, name: str, value: float, lo: float, hi: float) -> float:
        spec = self.specs.get(name)
        if spec and spec.min is not None:
            lo = max(lo, spec.min)
        if spec and spec.max is not None:
            hi = min(hi, spec.max)
        return max(lo, min(hi, value))

    def lookup_name(self, name: str, value: str) -> str:
        """Header values of lookup settings are numeric indexes; map them to names."""
        spec = self.specs.get(name)
        if spec and spec.allowed and value.isdigit() and int(value) < len(spec.allowed):
            return spec.allowed[int(value)]
        return value


# --------------------------------------------------------------------------
# CLI output parsers
# --------------------------------------------------------------------------
_SET_RE = re.compile(r"^set\s+(\S+)\s*=\s*(.*)$")
_VALUE_RE = re.compile(r"^(\w+)\s*=\s*(.*)$")


def parse_get_output(text: str) -> FCConfig:
    """Parse the output of ``get`` (no argument) into specs + values."""
    cfg = FCConfig(source="cli-get")
    cur: SettingSpec | None = None
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = _VALUE_RE.match(line)
        if m and not line.startswith(("Allowed", "Default", "Array")):
            cur = SettingSpec(m.group(1), m.group(2).strip())
            cfg.specs[cur.name] = cur
            cfg.values[cur.name] = cur.value
            continue
        if cur is None:
            continue
        m = re.match(r"^(profile|rateprofile|battery_profile)\s+(\d+)", line)
        if m:
            cur.section = m.group(1)
            if m.group(1) == PROFILE:
                cfg.pid_profile = int(m.group(2))
            elif m.group(1) == RATEPROFILE:
                cfg.rate_profile = int(m.group(2))
            continue
        m = re.match(r"^Allowed range:\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)", line)
        if m:
            cur.min, cur.max = float(m.group(1)), float(m.group(2))
            continue
        m = re.match(r"^Allowed values:\s*(.*)$", line)
        if m:
            cur.allowed = [a.strip() for a in m.group(1).split(",") if a.strip()]
            continue
        m = re.match(r"^Array length:\s*(\d+)", line)
        if m:
            cur.array_length = int(m.group(1))
            continue
        m = re.match(r"^Default value:\s*(.*)$", line)
        if m:
            cur.default = m.group(1).strip()
            continue
        m = re.match(r"^String length:", line)
        if m:
            cur.is_string = True
    for name, spec in cfg.specs.items():
        cfg.sections[name] = spec.section
    return cfg


def parse_diff_output(text: str) -> FCConfig:
    """Parse ``diff all`` / ``dump all``. Values of the *selected* profiles are kept."""
    cfg = FCConfig(source="cli-diff")
    section = MASTER
    index = 0
    per_profile: dict[tuple[str, int], dict[str, str]] = {}
    selected = {PROFILE: 0, RATEPROFILE: 0}
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            if line.startswith("# version"):
                continue
            continue
        m = re.match(r"^(profile|rateprofile|battery_profile)\s+(\d+)$", line)
        if m:
            section, index = m.group(1), int(m.group(2))
            if section in selected:
                selected[section] = index  # the last selection line wins (restore original selection)
            continue
        m = _SET_RE.match(line)
        if m:
            name, value = m.group(1), m.group(2).strip()
            per_profile.setdefault((section, index), {})[name] = value
            cfg.sections[name] = section
            continue
        m = re.match(r"^board_name\s+(\S+)", line)
        if m:
            cfg.values["board_name"] = m.group(1)
    for (section, index), vals in per_profile.items():
        if section == MASTER or selected.get(section, 0) == index or section == BATTERY:
            cfg.values.update(vals)
    cfg.pid_profile = selected[PROFILE]
    cfg.rate_profile = selected[RATEPROFILE]
    return cfg


def merge(primary: FCConfig, secondary: FCConfig) -> FCConfig:
    out = FCConfig(values=dict(secondary.values), specs=dict(secondary.specs),
                   sections=dict(secondary.sections), pid_profile=primary.pid_profile,
                   rate_profile=primary.rate_profile, source=f"{primary.source}+{secondary.source}",
                   firmware=primary.firmware or secondary.firmware)
    out.values.update(primary.values)
    out.specs.update(primary.specs)
    out.sections.update(primary.sections)
    return out


# --------------------------------------------------------------------------
# Blackbox headers -> CLI names
# --------------------------------------------------------------------------
_AXES = ("roll", "pitch", "yaw")
_HEADER_MULTI = {
    "rollPID": ["p_roll", "i_roll", "d_roll"],
    "pitchPID": ["p_pitch", "i_pitch", "d_pitch"],
    "yawPID": ["p_yaw", "i_yaw", "d_yaw"],
    "levelPID": ["angle_p_gain", "angle_i_gain", "angle_d_gain"],
    "ff_weight": ["f_roll", "f_pitch", "f_yaw"],
    "rc_rates": [f"{a}_rc_rate" for a in _AXES],
    "rc_expo": [f"{a}_expo" for a in _AXES],
    "rates": [f"{a}_srate" for a in _AXES],
    "rate_limits": [f"{a}_rate_limit" for a in _AXES],
    "gyro_lpf1_dyn_hz": ["gyro_lpf1_dyn_min_hz", "gyro_lpf1_dyn_max_hz"],
    "dterm_lpf1_dyn_hz": ["dterm_lpf1_dyn_min_hz", "dterm_lpf1_dyn_max_hz"],
    "gyro_notch_hz": ["gyro_notch1_hz", "gyro_notch2_hz"],
    "gyro_notch_cutoff": ["gyro_notch1_cutoff", "gyro_notch2_cutoff"],
    "vbatcellvoltage": ["vbat_min_cell_voltage", "vbat_warning_cell_voltage", "vbat_max_cell_voltage"],
}
_HEADER_RENAME = {
    "anti_gravity_threshold": "anti_gravity_threshold",
    "motor_idle": "dshot_idle_value",
}


def from_blackbox_headers(headers: dict[str, str]) -> FCConfig:
    cfg = FCConfig(source="blackbox-header", firmware=headers.get("Firmware revision", ""))
    fw = re.search(r"(\d+)\.(\d+)", cfg.firmware)
    version = (int(fw.group(1)), int(fw.group(2))) if fw else (4, 5)
    multi = dict(_HEADER_MULTI)
    # D-max / D-min naming changed in 4.6 (2025.12): header "d_max" = d_max_<axis>
    multi["d_min"] = [f"d_min_{a}" for a in _AXES]
    multi["d_max"] = [f"d_max_{a}" for a in _AXES]
    for name, value in headers.items():
        if name in multi:
            for cli_name, part in zip(multi[name], value.split(",")):
                cfg.values[cli_name] = part.strip()
                cfg.sections[cli_name] = guess_section(cli_name)
        elif re.match(r"^[a-z0-9_]+$", name):
            cli_name = _HEADER_RENAME.get(name, name)
            cfg.values[cli_name] = value.strip()
            cfg.sections[cli_name] = guess_section(cli_name)
    if version < (4, 3):
        cfg.values.setdefault("_legacy_firmware", "1")
    return cfg


_PROFILE_PREFIXES = (
    "p_", "i_", "d_", "f_", "d_min", "d_max", "dterm_", "iterm_", "anti_gravity", "feedforward_", "tpa_",
    "pidsum_", "yaw_lowpass", "abs_control", "use_integrated_yaw", "throttle_boost", "acro_trainer",
    "launch_control", "thrust_linear", "dyn_idle", "simplified_", "motor_output_limit", "auto_profile",
    "angle_", "horizon_", "level_", "vbat_sag", "ez_landing", "spa_", "crash_", "transient", "profile_name",
    "tpa", "s_", "landing_disarm", "chirp_",
)
_RATE_PREFIXES = ("roll_", "pitch_", "yaw_", "rates_type", "thr_mid", "thr_expo", "throttle_limit",
                  "rate_profile_name", "quickrates_rc_expo", "levelmode", "rateprofile_name")
_MASTER_EXCEPTIONS = {"yaw_motors_reversed", "yaw_control_reversed", "yaw_deadband", "roll_srate_limit",
                      "dterm_lpf_hz", "d_cutoff"}


def guess_section(name: str) -> str:
    """Best-effort section guess when the FC did not tell us (offline mode)."""
    if name in _MASTER_EXCEPTIONS:
        return MASTER
    if name.startswith(("roll_", "pitch_", "yaw_")) and name.endswith(("rc_rate", "expo", "srate", "rate_limit")):
        return RATEPROFILE
    if name.startswith(_RATE_PREFIXES) and not name.startswith(("yaw_lowpass",)):
        if name in ("rates_type", "thr_mid", "thr_expo", "throttle_limit_type", "throttle_limit_percent",
                    "quickrates_rc_expo", "rate_profile_name", "levelmode"):
            return RATEPROFILE
    if name.startswith(_PROFILE_PREFIXES) and not name.startswith(("dterm_notch_hz_legacy",)):
        return PROFILE
    return MASTER

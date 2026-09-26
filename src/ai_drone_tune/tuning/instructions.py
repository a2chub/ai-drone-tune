"""Manual mode: turn user instructions into setting changes.

Understands (Japanese and English):

* direct CLI assignments   ``p_roll=52``, ``set d_pitch = 38``, ``dyn_notch_count 2``
* relative adjustments      ``p_roll +10%``, ``d_pitch -3``, ``f_yaw *1.2``
* axis/term shortcuts       ``ロールのPを5%上げて``, ``pitch D down 10%``, ``yaw I +5``
* rate targets              ``最大レート 800``, ``roll rate 750``, ``yaw max 600deg/s``
* intents                   ``プロップウォッシュを減らしたい``, ``sharper``, ``motors are hot`` ...

Anything the rules cannot map can optionally be sent to Claude
(``--llm``) which returns the same structured change list; every result goes
through the normal validation / approval flow.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .changes import Change, ChangeSet
from .config import FCConfig
from .rates import RATES_TYPES, AxisRates, describe, solve_for_max_rate

AXIS_WORDS = {
    "roll": ("roll", "ロール"),
    "pitch": ("pitch", "ピッチ"),
    "yaw": ("yaw", "ヨー"),
}
UP_WORDS = ("上げ", "あげ", "増や", "強く", "強め", "高く", "up", "increase", "raise", "more", "higher", "boost")
DOWN_WORDS = ("下げ", "さげ", "減ら", "弱く", "弱め", "低く", "down", "decrease", "lower", "less", "reduce")
SMALL_WORDS = ("少し", "ちょっと", "わずか", "slightly", "a bit", "a little", "bit")
BIG_WORDS = ("かなり", "大きく", "大幅", "a lot", "much", "significantly")

_ISO = r"(?<![a-z0-9_])%s(?![a-z0-9_])"
TERM_PATTERNS = [
    ("f", r"(?:feed\s*forward|フィードフォワード|" + _ISO % "ff" + "|" + _ISO % "f" + ")"),
    ("p", _ISO % "p"),
    ("i", _ISO % "i"),
    ("d", _ISO % "d"),
]


@dataclass
class ParseResult:
    changes: ChangeSet
    unparsed: list[str]
    messages: list[str]


def _magnitude(text: str, default: float = 0.10) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|％|パーセント|percent)", text)
    if m:
        return float(m.group(1)) / 100.0
    if any(w in text for w in SMALL_WORDS):
        return 0.05
    if any(w in text for w in BIG_WORDS):
        return 0.15
    return default


def _direction(text: str) -> int:
    t = text.lower()
    up = any(w in t for w in UP_WORDS) or re.search(r"\+\s*\d", t) is not None
    down = any(w in t for w in DOWN_WORDS) or re.search(r"(?<![\w=])-\s*\d", t) is not None
    if up and not down:
        return 1
    if down and not up:
        return -1
    return 0


def _axes(text: str, default=("roll", "pitch")) -> list[str]:
    t = text.lower()
    found = [ax for ax, words in AXIS_WORDS.items() if any(w in t for w in words)]
    if any(w in t for w in ("全軸", "all axes", "全部")):
        return ["roll", "pitch", "yaw"]
    return found or list(default)


class InstructionParser:
    def __init__(self, cfg: FCConfig):
        self.cfg = cfg
        self.cs = ChangeSet()
        self.messages: list[str] = []

    # ------------------------------------------------------------------
    def _cur(self, name: str) -> float | None:
        pending = self.cs.get(name)
        v = pending.new if pending else self.cfg.get(name)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _set(self, name: str, value, reason: str, category: str = "manual") -> None:
        if isinstance(value, float):
            value = int(round(value))
        spec = self.cfg.specs.get(name)
        if isinstance(value, int) and spec and spec.min is not None:
            value = int(max(spec.min, min(spec.max, value)))
        pending = self.cs.get(name)
        old = pending.old if pending else self.cfg.get(name)
        self.cs.add(Change(name, old, str(value), reason, category, 1.0, self.cfg.section_of(name)))

    def _scale(self, name: str, factor: float, reason: str, category: str = "manual") -> None:
        cur = self._cur(name)
        if cur is None:
            self.messages.append(f"{name} is not available on this FC")
            return
        new = cur * factor
        if abs(new - cur) < 1 and factor != 1:
            new = cur + (1 if factor > 1 else -1)
        self._set(name, new, reason, category)

    def _d_names(self, axis: str) -> list[str]:
        names = [f"d_{axis}"]
        for extra in (f"d_max_{axis}", f"d_min_{axis}"):
            if (self.cfg.get_int(extra) or 0) > 0:
                names.append(extra)
        return names

    # ------------------------------------------------------------------
    def parse(self, text: str) -> ParseResult:
        unparsed = []
        for part in re.split(r"[\n;、。,]+|\s+and\s+|そして|、", text):
            part = part.strip()
            if not part:
                continue
            if not self._parse_one(part):
                unparsed.append(part)
        return ParseResult(self.cs, unparsed, self.messages)

    def _parse_one(self, text: str) -> bool:
        return (self._direct(text) or self._rates(text) or self._term(text) or self._master(text)
                or self._intent(text))

    # --- direct CLI syntax ------------------------------------------------
    def _direct(self, text: str) -> bool:
        m = re.match(r"^(?:set\s+)?([a-z][a-z0-9_]+)\s*(?:=|\s)\s*([+\-*/]?)\s*(-?[\w.,]+)\s*(%|％)?\s*$",
                     text.strip(), flags=re.I)
        if not m:
            return False
        name, op, val, pct = m.group(1).lower(), m.group(2), m.group(3), m.group(4)
        if name not in self.cfg.values and name not in self.cfg.specs:
            return False
        if op in ("+", "-") or pct:
            try:
                num = float(val)
            except ValueError:
                return False
            cur = self._cur(name)
            if cur is None:
                return False
            sign = -1 if op == "-" else 1
            new = cur * (1 + sign * num / 100.0) if pct else cur + sign * num
            self._set(name, new, f"manual: {text}")
        elif op == "*":
            self._scale(name, float(val), f"manual: {text}")
        elif op == "/":
            self._scale(name, 1.0 / float(val), f"manual: {text}")
        else:
            spec = self.cfg.specs.get(name)
            value = val
            if spec and spec.allowed:
                match = [a for a in spec.allowed if a.upper() == val.upper()]
                value = match[0] if match else val
            self._set(name, value, f"manual: {text}")
        return True

    # --- rates ------------------------------------------------------------
    def _rates(self, text: str) -> bool:
        t = text.lower()
        if not re.search(r"(rate|レート|回転|deg/s|度)", t):
            return False
        m = re.search(r"(\d{3,4})\s*(?:deg/s|°/s|度|dps)?", t)
        if not m:
            return False
        target = float(m.group(1))
        rates_type = self.cfg.lookup_name("rates_type", str(self.cfg.get("rates_type", "ACTUAL"))).upper()
        if rates_type not in RATES_TYPES:
            rates_type = "ACTUAL"
        axes = _axes(text, default=("roll", "pitch"))
        for ax in axes:
            r = AxisRates(int(self._cur(f"{ax}_rc_rate") or 0), int(self._cur(f"{ax}_srate") or 0),
                          int(self._cur(f"{ax}_expo") or 0), int(self._cur(f"{ax}_rate_limit") or 1998))
            new = solve_for_max_rate(rates_type, r, target)
            self._set(f"{ax}_srate", new.srate, f"{ax} max rate -> {target:.0f} deg/s ({rates_type})", "rates")
            self.messages.append(f"{ax}: {describe(rates_type, new)}")
        return True

    # --- P/I/D/F on axes ----------------------------------------------------
    def _term(self, text: str) -> bool:
        t = text.lower()
        terms = [name for name, pat in TERM_PATTERNS if re.search(pat, t)]
        if not terms:
            return False
        direction = _direction(t)
        abs_m = re.search(r"(?:を|to|=)\s*(\d+)\s*(?:に|$)", t)
        if direction == 0 and not abs_m:
            return False
        axes = _axes(t)
        for term in terms[:1] if len(terms) > 1 and "f" in terms else terms:
            for ax in axes:
                names = self._d_names(ax) if term == "d" else [f"{term}_{ax}"]
                for name in names:
                    if abs_m and direction == 0:
                        self._set(name, int(abs_m.group(1)), f"manual: {text}")
                    else:
                        m = re.search(r"([+\-])?\s*(\d+)(?!\s*(?:%|％|パーセント))\b", t)
                        if m and not re.search(r"(%|％|パーセント)", t) and not any(w in t for w in SMALL_WORDS + BIG_WORDS):
                            cur = self._cur(name)
                            if cur is not None:
                                self._set(name, cur + direction * int(m.group(2)), f"manual: {text}")
                        else:
                            self._scale(name, 1 + direction * _magnitude(t), f"manual: {text}")
        return True

    def _master(self, text: str) -> bool:
        t = text.lower()
        if not any(w in t for w in ("master", "マスター", "全体", "overall")):
            return False
        d = _direction(t)
        if d == 0:
            return False
        f = 1 + d * _magnitude(t)
        for ax in ("roll", "pitch", "yaw"):
            for term in ("p", "i", "f"):
                self._scale(f"{term}_{ax}", f, f"manual: {text}")
            if ax != "yaw":
                for name in self._d_names(ax):
                    self._scale(name, f, f"manual: {text}")
        return True

    # --- intents ------------------------------------------------------------
    def _intent(self, text: str) -> bool:
        t = text.lower()
        mag = _magnitude(t)
        axes = _axes(t)
        if re.search(r"propwash|プロペラウォッシュ|プロップウォッシュ|ウォッシュ", t):
            for ax in [a for a in axes if a != "yaw"]:
                base = f"d_min_{ax}" if (self.cfg.get_int(f"d_min_{ax}") or 0) > 0 else f"d_{ax}"
                self._scale(base, 1 + mag, "propwash: more (base) D", "pid")
            if (self.cfg.get_int("dyn_idle_min_rpm") or 0) == 0 and self.cfg.is_on("dshot_bidir"):
                self._set("dyn_idle_min_rpm", 30, "propwash: enable dynamic idle", "other")
            return True
        if re.search(r"bounce|overshoot|バウンス|オーバーシュート|跳ね", t):
            for ax in axes:
                for n in self._d_names(ax):
                    self._scale(n, 1 + mag, "reduce bounce-back: more D", "pid")
            return True
        if re.search(r"hot|熱|発熱", t):
            for ax in [a for a in axes if a != "yaw"]:
                for n in self._d_names(ax):
                    self._scale(n, 1 - mag, "hot motors: less D", "pid")
            for n in ("dterm_lpf1_dyn_min_hz", "dterm_lpf1_dyn_max_hz", "dterm_lpf2_static_hz"):
                if (self.cfg.get_int(n) or 0) > 0:
                    self._scale(n, 1 - mag, "hot motors: more D-term filtering", "filter")
            return True
        if re.search(r"noise|jello|ノイズ|ジェロ|振動|ブレ", t):
            for n in ("gyro_lpf1_dyn_min_hz", "gyro_lpf1_dyn_max_hz", "gyro_lpf2_static_hz",
                      "dterm_lpf1_dyn_min_hz", "dterm_lpf1_dyn_max_hz", "dterm_lpf2_static_hz"):
                if (self.cfg.get_int(n) or 0) > 0:
                    self._scale(n, 1 - mag, "noise: more filtering", "filter")
            return True
        if re.search(r"latency|delay|遅延|レイテンシ|もっさり", t):
            for n in ("gyro_lpf1_dyn_min_hz", "gyro_lpf1_dyn_max_hz", "gyro_lpf2_static_hz",
                      "dterm_lpf1_dyn_min_hz", "dterm_lpf1_dyn_max_hz", "dterm_lpf2_static_hz"):
                if (self.cfg.get_int(n) or 0) > 0:
                    self._scale(n, 1 + mag, "less filter delay", "filter")
            return True
        if re.search(r"sharp|crisp|snappy|locked|キビキビ|シャープ|機敏|クイック|応答を速", t):
            for ax in axes:
                self._scale(f"p_{ax}", 1 + mag, "sharper response: more P", "pid")
                self._scale(f"f_{ax}", 1 + mag, "sharper response: more feedforward", "feedforward")
            return True
        if re.search(r"smooth|soft|cinematic|マイルド|滑らか|なめらか|柔らか", t):
            for ax in axes:
                self._scale(f"p_{ax}", 1 - mag, "smoother: less P", "pid")
                self._scale(f"f_{ax}", 1 - mag, "smoother: less feedforward", "feedforward")
            return True
        if re.search(r"drift|ドリフト|流れ|holds? (?:angle|line)|ライン", t):
            for ax in _axes(t, default=("roll", "pitch", "yaw")):
                self._scale(f"i_{ax}", 1 + mag, "better attitude hold: more I", "pid")
            return True
        if re.search(r"high throttle|punch|高スロットル|パンチ|全開", t):
            cur = self._cur("tpa_rate")
            if cur is not None:
                self._set("tpa_rate", min(100, cur + 10), "high-throttle oscillation: more TPA", "pid")
            return True
        return False


# --------------------------------------------------------------------------
# Optional Claude backend
# --------------------------------------------------------------------------
LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["name", "value", "reason"],
                "additionalProperties": False,
            },
        },
        "explanation": {"type": "string"},
    },
    "required": ["changes", "explanation"],
    "additionalProperties": False,
}

LLM_SYSTEM = """You are an expert Betaflight FPV racing-quad tuner.
The user describes how their quad flies or what they want changed. Propose Betaflight CLI setting changes.
Rules:
- Only use setting names that appear in the provided current configuration.
- Prefer small steps (<= 15 % per setting) unless the user asks for a specific value.
- Keep P/D balance in mind; do not raise D when D-term noise is already high.
- Rates are pilot preference: change them only when asked.
- Values must be plain CLI values (numbers, or lookup names such as ON/OFF).
Answer in the user's language in `explanation`."""


def llm_parse(text: str, cfg: FCConfig, analysis_summary: dict | None = None,
              model: str = "claude-opus-5") -> tuple[ChangeSet, str]:
    """Ask Claude to translate a free-form instruction into setting changes."""
    import anthropic

    relevant = {k: v for k, v in sorted(cfg.values.items()) if not k.startswith("_")}
    context = {"current_config": relevant, "pid_profile": cfg.pid_profile, "rate_profile": cfg.rate_profile}
    if analysis_summary:
        context["latest_blackbox_analysis"] = analysis_summary
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        system=LLM_SYSTEM,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": LLM_SCHEMA}},
        messages=[{"role": "user", "content": json.dumps(context, ensure_ascii=False) + "\n\nInstruction: " + text}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined this request")
    raw = next(b.text for b in response.content if b.type == "text")
    data = json.loads(raw)
    cs = ChangeSet()
    for item in data.get("changes", []):
        name = item["name"].strip()
        if name not in cfg.values:
            cs.notes.append(f"ignored unknown setting from AI: {name}")
            continue
        cs.add(Change(name, cfg.get(name), item["value"].strip(), f"AI: {item['reason']}", "manual", 0.7,
                      cfg.section_of(name)))
    return cs, data.get("explanation", "")

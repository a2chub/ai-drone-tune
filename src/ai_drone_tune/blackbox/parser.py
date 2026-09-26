"""Betaflight blackbox (.bbl / .bfl) log parser.

A file may contain several logs (one per arm/disarm cycle); each starts with
the ``H Product:Blackbox flight data recorder`` header line.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .stream import ByteStream, StreamEOF, sign_extend

LOG_START_MARKER = b"H Product:Blackbox flight data recorder by Nicholas Sherlock"

# Predictors
P_ZERO, P_PREVIOUS, P_STRAIGHT_LINE, P_AVERAGE_2, P_MINTHROTTLE, P_MOTOR_0, P_INC, \
    P_HOME_COORD, P_1500, P_VBATREF, P_LAST_MAIN_FRAME_TIME, P_MINMOTOR = range(12)
P_HOME_COORD_1 = 256

# Encodings
E_SIGNED_VB, E_UNSIGNED_VB, E_NEG_14BIT = 0, 1, 3
E_TAG8_8SVB, E_TAG2_3S32, E_TAG8_4S16, E_NULL, E_TAG2_3SVARIABLE = 6, 7, 8, 9, 10

# Events
EV_SYNC_BEEP = 0
EV_AUTOTUNE_CYCLE_START = 10
EV_AUTOTUNE_CYCLE_RESULT = 11
EV_AUTOTUNE_TARGETS = 12
EV_INFLIGHT_ADJUSTMENT = 13
EV_LOGGING_RESUME = 14
EV_DISARM = 15
EV_GTUNE_CYCLE_RESULT = 20
EV_FLIGHT_MODE = 30
EV_TWITCH_TEST = 40
EV_LOG_END = 255

MAX_FRAME_LENGTH = 256
MAX_ITERATION_JUMP = 500 * 10
MAX_TIME_JUMP = 10 * 1_000_000


class ParseError(Exception):
    pass


@dataclass
class FrameDef:
    names: list[str] = field(default_factory=list)
    signed: list[int] = field(default_factory=list)
    predictor: list[int] = field(default_factory=list)
    encoding: list[int] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.names)

    def complete(self) -> bool:
        return self.count > 0 and len(self.predictor) == self.count and len(self.encoding) == self.count

    def index(self, name: str) -> int | None:
        try:
            return self.names.index(name)
        except ValueError:
            return None


@dataclass
class Event:
    time_us: int | None
    type: int
    data: dict


@dataclass
class FlightLog:
    """One decoded log (a single arm..disarm recording)."""

    index: int
    headers: dict[str, str]
    field_names: list[str]
    frames: np.ndarray  # shape (n_frames, n_fields), int64
    slow_names: list[str] = field(default_factory=list)
    slow_frames: np.ndarray | None = None  # (n, 1 + n_slow): first column is time_us
    gps_names: list[str] = field(default_factory=list)
    gps_frames: np.ndarray | None = None
    events: list[Event] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    byte_size: int = 0  # size of this log inside the file (for data-rate estimates)

    # ------------------------------------------------------------------
    def __contains__(self, name: str) -> bool:
        return name in self.field_names

    def __getitem__(self, name: str) -> np.ndarray:
        return self.frames[:, self.field_names.index(name)]

    def get(self, name: str, default=None):
        return self[name] if name in self.field_names else default

    def axis(self, prefix: str) -> np.ndarray | None:
        """Return an (n, k) array for ``prefix[0]``, ``prefix[1]``... fields."""
        cols = []
        i = 0
        while f"{prefix}[{i}]" in self.field_names:
            cols.append(self[f"{prefix}[{i}]"])
            i += 1
        return np.stack(cols, axis=1) if cols else None

    @property
    def time_us(self) -> np.ndarray:
        return self["time"]

    @property
    def time_s(self) -> np.ndarray:
        t = self.time_us.astype(np.float64)
        return (t - t[0]) / 1e6 if len(t) else t

    @property
    def duration_s(self) -> float:
        t = self.time_us
        return float(t[-1] - t[0]) / 1e6 if len(t) > 1 else 0.0

    @property
    def sample_rate_hz(self) -> float:
        t = self.time_us
        if len(t) < 3:
            return 0.0
        dt = np.median(np.diff(t))
        return 1e6 / dt if dt > 0 else 0.0

    # --- configuration helpers ---------------------------------------
    @property
    def firmware_revision(self) -> str:
        return self.headers.get("Firmware revision", "")

    @property
    def firmware_version(self) -> tuple[int, ...]:
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", self.firmware_revision)
        if m:
            return tuple(int(x) for x in m.groups())
        m = re.search(r"(\d{4})\.(\d+)\.(\d+)", self.firmware_revision)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

    @property
    def craft_name(self) -> str:
        return self.headers.get("Craft name", "")

    def header_int(self, name: str, default: int | None = None) -> int | None:
        v = self.headers.get(name)
        if v is None:
            return default
        try:
            return int(v.split(",")[0], 0)
        except ValueError:
            return default

    def header_ints(self, name: str) -> list[int]:
        v = self.headers.get(name)
        if not v:
            return []
        out = []
        for part in v.split(","):
            try:
                out.append(int(part, 0))
            except ValueError:
                pass
        return out


def _hex_to_float(s: str) -> float:
    return struct.unpack("<f", struct.pack("<I", int(s, 16)))[0]


class _LogParser:
    def __init__(self, data: bytes, start: int, end: int, index: int):
        self.data = data
        self.start = start
        self.end = end
        self.index = index
        self.headers: dict[str, str] = {}
        self.defs: dict[str, FrameDef] = {}
        self.data_version = 2
        self.minthrottle = 1150
        self.minmotor = 0
        self.vbatref = 4095
        self.frame_interval_i = 32
        self.frame_interval_p_num = 1
        self.frame_interval_p_denom = 1

    # ------------------------------------------------------------------
    def parse_headers(self) -> int:
        data = self.data
        pos = self.start
        while pos < self.end and data[pos:pos + 2] == b"H ":
            nl = data.find(b"\n", pos, self.end)
            if nl < 0:
                nl = self.end
            line = data[pos + 2:nl].decode("latin-1").rstrip("\r")
            pos = nl + 1
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            self._header(name, value)
        if "motorOutput" not in self.headers:
            self.minmotor = self.minthrottle
        return pos

    def _header(self, name: str, value: str) -> None:
        self.headers[name] = value
        m = re.match(r"Field (\w) (name|signed|predictor|encoding)$", name)
        if m:
            ftype, attr = m.groups()
            d = self.defs.setdefault(ftype, FrameDef())
            if attr == "name":
                d.names = value.split(",")
                if ftype == "I":
                    self.defs.setdefault("P", FrameDef()).names = d.names
            else:
                ints = [int(x) for x in value.split(",") if x != ""]
                setattr(d, attr if attr != "signed" else "signed", ints)
                if ftype == "I" and attr == "signed":
                    self.defs.setdefault("P", FrameDef()).signed = ints
            return
        try:
            if name == "Data version":
                self.data_version = int(value)
            elif name == "minthrottle":
                self.minthrottle = int(value)
            elif name == "motorOutput":
                self.minmotor = int(value.split(",")[0])
            elif name == "vbatref":
                self.vbatref = int(value)
            elif name == "I interval":
                self.frame_interval_i = max(1, int(value))
            elif name == "P interval":
                m2 = re.match(r"(\d+)/(\d+)", value)
                if m2:
                    self.frame_interval_p_num = int(m2.group(1))
                    self.frame_interval_p_denom = int(m2.group(2))
                else:
                    self.frame_interval_p_num = 1
                    self.frame_interval_p_denom = max(1, int(value))
        except ValueError:
            pass

    # ------------------------------------------------------------------
    def should_have_frame(self, idx: int) -> bool:
        return ((idx % self.frame_interval_i + self.frame_interval_p_num - 1)
                % self.frame_interval_p_denom) < self.frame_interval_p_num

    def count_skipped(self, last_iter: int) -> int:
        if last_iter < 0:
            return 0
        count = 0
        idx = last_iter + 1
        while not self.should_have_frame(idx) and count < 10000:
            count += 1
            idx += 1
        return count

    # ------------------------------------------------------------------
    def parse_frame(self, s: ByteStream, fdef: FrameDef, previous, previous2, skipped: int,
                    home=None, last_main_time: int = 0) -> list[int]:
        n = fdef.count
        pred = fdef.predictor
        enc = fdef.encoding
        current = [0] * n
        i = 0
        while i < n:
            p = pred[i]
            if p == P_INC:
                current[i] = skipped + 1 + (previous[i] if previous is not None else 0)
                i += 1
                continue
            e = enc[i]
            if e == E_SIGNED_VB:
                values = (s.read_signed_vb(),)
            elif e == E_UNSIGNED_VB:
                u = s.read_unsigned_vb()
                # like the reference viewer, treat values as int32 (motor[0] residuals can be negative)
                values = (u - 0x100000000 if u >= 0x80000000 else u,)
            elif e == E_TAG8_4S16:
                values = s.read_tag8_4s16_v2() if self.data_version >= 2 else s.read_tag8_4s16_v1()
            elif e == E_TAG2_3S32:
                values = s.read_tag2_3s32()
            elif e == E_TAG8_8SVB:
                j = i + 1
                while j < i + 8 and j < n and enc[j] == E_TAG8_8SVB:
                    j += 1
                values = s.read_tag8_8svb(j - i)
            elif e == E_NULL:
                values = (0,)
            elif e == E_NEG_14BIT:
                values = (-sign_extend(s.read_unsigned_vb(), 14),)
            elif e == E_TAG2_3SVARIABLE:
                values = s.read_tag2_3svariable()
            else:
                raise ParseError(f"unsupported encoding {e}")
            for v in values:
                if i >= n:
                    break
                p = pred[i]
                if p == P_ZERO:
                    pass
                elif p == P_PREVIOUS:
                    if previous is not None:
                        v += previous[i]
                elif p == P_STRAIGHT_LINE:
                    if previous is not None:
                        v += 2 * previous[i] - previous2[i]
                elif p == P_AVERAGE_2:
                    if previous is not None:
                        v += int((previous[i] + previous2[i]) / 2)
                elif p == P_MINTHROTTLE:
                    v += self.minthrottle
                elif p == P_MINMOTOR:
                    v += self.minmotor
                elif p == P_MOTOR_0:
                    m0 = self.motor0_index
                    if m0 is not None:
                        v += current[m0]
                elif p == P_VBATREF:
                    v += self.vbatref
                elif p == P_1500:
                    v += 1500
                elif p == P_HOME_COORD:
                    if home is not None:
                        v += home[0]
                elif p == P_HOME_COORD_1:
                    if home is not None and len(home) > 1:
                        v += home[1]
                elif p == P_LAST_MAIN_FRAME_TIME:
                    v += last_main_time
                elif p == P_INC:
                    v += skipped + 1 + (previous[i] if previous is not None else 0)
                else:
                    raise ParseError(f"unsupported predictor {p}")
                current[i] = v
                i += 1
        return current

    # ------------------------------------------------------------------
    def parse(self) -> FlightLog:
        data_start = self.parse_headers()
        i_def = self.defs.get("I")
        p_def = self.defs.get("P")
        if i_def is None or not i_def.complete():
            raise ParseError("log has no usable I frame definition")
        if p_def is None or not p_def.complete():
            p_def = None
        self.motor0_index = i_def.index("motor[0]")
        g_def = self.defs.get("G")
        if g_def is not None and g_def.complete():
            for k in range(1, g_def.count):
                if g_def.predictor[k - 1] == P_HOME_COORD and g_def.predictor[k] == P_HOME_COORD:
                    g_def.predictor[k] = P_HOME_COORD_1
        else:
            g_def = None
        h_def = self.defs.get("H") if self.defs.get("H") and self.defs["H"].complete() else None
        s_def = self.defs.get("S") if self.defs.get("S") and self.defs["S"].complete() else None

        time_idx = i_def.index("time")
        iter_idx = i_def.index("loopIteration")
        if time_idx is None or iter_idx is None:
            raise ParseError("log is missing loopIteration/time fields")

        s = ByteStream(self.data, data_start, self.end)
        frames: list[list[int]] = []
        slow_frames: list[list[int]] = []
        gps_frames: list[list[int]] = []
        events: list[Event] = []
        stats = {"corrupt": 0, "desync": 0, "I": 0, "P": 0, "E": 0, "S": 0, "G": 0, "H": 0}

        prev = prev2 = None  # history for P frames
        main_valid = False
        last_iter = -1
        last_time = -1
        gps_home: list[int] | None = None
        last_gps: list[int] | None = None

        pending = None  # (type, values, extra)
        frame_start = s.pos
        premature_eof = False
        frame_types = {ord("I"), ord("P"), ord("E"), ord("S"), ord("G"), ord("H")}
        log_ended = False

        while True:
            if s.pos < s.end and not log_ended:
                cmd = s.data[s.pos]
                s.pos += 1
            else:
                cmd = -1

            if pending is not None:
                frame_size = s.pos - frame_start
                looks_complete = cmd in frame_types or (cmd == -1 and not premature_eof)
                if frame_size <= MAX_FRAME_LENGTH + 1 and looks_complete:
                    ftype, values, extra = pending
                    if ftype == "I":
                        it, t = values[iter_idx], values[time_idx]
                        accept = True
                        if last_iter != -1:
                            accept = (last_iter <= it < last_iter + MAX_ITERATION_JUMP
                                      and last_time <= t < last_time + MAX_TIME_JUMP)
                        if accept:
                            last_iter, last_time = it, t
                            main_valid = True
                            frames.append(values)
                            stats["I"] += 1
                            prev = prev2 = values
                        else:
                            main_valid = False
                            prev = prev2 = None
                            stats["desync"] += 1
                    elif ftype == "P":
                        if main_valid and prev is not None:
                            it, t = values[iter_idx], values[time_idx]
                            if (it >= last_iter and t >= last_time and it < last_iter + MAX_ITERATION_JUMP
                                    and t < last_time + MAX_TIME_JUMP):
                                last_iter, last_time = it, t
                                frames.append(values)
                                stats["P"] += 1
                                prev2, prev = prev, values
                            else:
                                main_valid = False
                                stats["desync"] += 1
                        else:
                            main_valid = False
                    elif ftype == "E":
                        ev = extra
                        if ev is not None:
                            events.append(ev)
                            stats["E"] += 1
                            if ev.type == EV_LOGGING_RESUME:
                                last_iter = ev.data["logIteration"]
                                last_time = ev.data["currentTime"]
                            if ev.type == EV_LOG_END:
                                log_ended = True
                    elif ftype == "S":
                        slow_frames.append([last_time] + values)
                        stats["S"] += 1
                    elif ftype == "H":
                        gps_home = values
                        stats["H"] += 1
                    elif ftype == "G":
                        last_gps = values
                        gps_frames.append(values)
                        stats["G"] += 1
                else:
                    stats["corrupt"] += 1
                    main_valid = False
                    s.pos = frame_start + 1
                    pending = None
                    premature_eof = False
                    continue
                pending = None

            if cmd == -1:
                break

            frame_start = s.pos - 1
            c = chr(cmd)
            try:
                if c == "I":
                    values = self.parse_frame(s, i_def, prev, prev2, 0)
                    pending = ("I", values, None)
                elif c == "P" and p_def is not None:
                    if prev is None:
                        # still need to consume the frame to stay in sync
                        values = self.parse_frame(s, p_def, None, None, 0)
                        pending = ("P", values, None)
                        main_valid = False
                    else:
                        skipped = self.count_skipped(last_iter)
                        values = self.parse_frame(s, p_def, prev, prev2, skipped)
                        pending = ("P", values, None)
                elif c == "E":
                    ev = self.parse_event(s, last_time)
                    pending = ("E", None, ev)
                elif c == "S" and s_def is not None:
                    pending = ("S", self.parse_frame(s, s_def, None, None, 0), None)
                elif c == "H" and h_def is not None:
                    pending = ("H", self.parse_frame(s, h_def, None, None, 0), None)
                elif c == "G" and g_def is not None:
                    pending = ("G", self.parse_frame(s, g_def, last_gps, last_gps, 0, home=gps_home,
                                                     last_main_time=last_time if last_time > 0 else 0), None)
                else:
                    main_valid = False
                    pending = None
            except StreamEOF:
                premature_eof = True
                pending = (c, [], None) if c != "E" else ("E", None, None)
                s.pos = s.end
            except ParseError:
                stats["corrupt"] += 1
                main_valid = False
                s.pos = frame_start + 1
                pending = None

        arr = np.array(frames, dtype=np.int64) if frames else np.zeros((0, i_def.count), dtype=np.int64)
        slow_arr = np.array(slow_frames, dtype=np.int64) if slow_frames else None
        gps_arr = np.array(gps_frames, dtype=np.int64) if gps_frames else None
        return FlightLog(
            index=self.index,
            headers=self.headers,
            field_names=list(i_def.names),
            frames=arr,
            slow_names=list(s_def.names) if s_def else [],
            slow_frames=slow_arr,
            gps_names=list(g_def.names) if g_def else [],
            gps_frames=gps_arr,
            events=events,
            stats=stats,
            byte_size=self.end - self.start,
        )

    # ------------------------------------------------------------------
    def parse_event(self, s: ByteStream, last_time: int) -> Event | None:
        etype = s.read_byte()
        data: dict = {}
        t = last_time if last_time >= 0 else None
        if etype == EV_SYNC_BEEP:
            data["time"] = s.read_unsigned_vb()
            t = data["time"]
        elif etype == EV_FLIGHT_MODE:
            data["newFlags"] = s.read_unsigned_vb()
            data["lastFlags"] = s.read_unsigned_vb()
        elif etype == EV_DISARM:
            data["reason"] = s.read_unsigned_vb()
        elif etype == EV_INFLIGHT_ADJUSTMENT:
            func = s.read_byte()
            data["func"] = func & 0x7F
            if func >= 128:
                data["value"] = struct.unpack("<f", s.read_bytes(4))[0]
            else:
                data["value"] = s.read_signed_vb()
        elif etype == EV_LOGGING_RESUME:
            data["logIteration"] = s.read_unsigned_vb()
            data["currentTime"] = s.read_unsigned_vb()
            t = data["currentTime"]
        elif etype == EV_AUTOTUNE_CYCLE_START:
            s.read_bytes(5)
        elif etype == EV_AUTOTUNE_CYCLE_RESULT:
            s.read_bytes(4)
        elif etype == EV_AUTOTUNE_TARGETS:
            s.read_bytes(8)
        elif etype == EV_GTUNE_CYCLE_RESULT:
            s.read_byte()
            s.read_signed_vb()
            s.read_bytes(2)
        elif etype == EV_TWITCH_TEST:
            s.read_byte()
            s.read_bytes(4)
        elif etype == EV_LOG_END:
            msg = s.read_bytes(11)
            if msg != b"End of log\x00":
                return None
        else:
            return None
        return Event(t, etype, data)


def split_logs(data: bytes) -> list[tuple[int, int]]:
    starts = [m.start() for m in re.finditer(re.escape(LOG_START_MARKER), data)]
    bounds = []
    for k, st in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(data)
        bounds.append((st, end))
    return bounds


def parse_bytes(data: bytes, min_frames: int = 1) -> list[FlightLog]:
    logs = []
    for idx, (st, end) in enumerate(split_logs(data)):
        try:
            log = _LogParser(data, st, end, idx).parse()
        except ParseError:
            continue
        if len(log.frames) >= min_frames:
            logs.append(log)
    return logs


def parse_file(path: str | Path, min_frames: int = 1) -> list[FlightLog]:
    return parse_bytes(Path(path).read_bytes(), min_frames=min_frames)

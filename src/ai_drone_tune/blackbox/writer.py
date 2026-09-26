"""Blackbox log *encoder* (inverse of the parser).

Produces files byte-compatible with the firmware encoder for the field layout
used by Betaflight 4.x. It is used to build synthetic logs for tests and for
the ``aidt simulate`` command (trying the analysis pipeline without a quad).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import parser as P


def _u_vb(value: int) -> bytes:
    value &= 0xFFFFFFFF
    out = bytearray()
    while value > 127:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _s_vb(value: int) -> bytes:
    return _u_vb(((value << 1) ^ (value >> 31)) & 0xFFFFFFFF)


def _tag8_4s16(values: list[int]) -> bytes:
    selector = 0
    for x in range(3, -1, -1):
        selector <<= 2
        v = values[x]
        if v == 0:
            pass
        elif -8 <= v < 8:
            selector |= 1
        elif -128 <= v < 128:
            selector |= 2
        else:
            selector |= 3
    out = bytearray([selector])
    nibble = 0
    buffer = 0
    sel = selector
    for x in range(4):
        kind = sel & 3
        v = values[x]
        if kind == 1:
            if nibble == 0:
                buffer = (v << 4) & 0xFF
                nibble = 1
            else:
                out.append(buffer | (v & 0x0F))
                nibble = 0
        elif kind == 2:
            if nibble == 0:
                out.append(v & 0xFF)
            else:
                out.append(buffer | ((v >> 4) & 0x0F))
                buffer = (v << 4) & 0xFF
        elif kind == 3:
            if nibble == 0:
                out.append((v >> 8) & 0xFF)
                out.append(v & 0xFF)
            else:
                out.append(buffer | ((v >> 12) & 0x0F))
                out.append((v >> 4) & 0xFF)
                buffer = (v << 4) & 0xFF
        sel >>= 2
    if nibble == 1:
        out.append(buffer)
    return bytes(out)


def _tag2_3s32(values: list[int]) -> bytes:
    selector = 0
    for v in values:
        if v >= 32 or v < -32:
            selector = 3
            break
        if v >= 8 or v < -8:
            selector = max(selector, 2)
        elif v >= 2 or v < -2:
            selector = max(selector, 1)
    if selector == 0:
        return bytes([((values[0] & 3) << 4) | ((values[1] & 3) << 2) | (values[2] & 3)])
    if selector == 1:
        return bytes([(1 << 6) | (values[0] & 0x0F), ((values[1] << 4) & 0xF0) | (values[2] & 0x0F)])
    if selector == 2:
        return bytes([(2 << 6) | (values[0] & 0x3F), values[1] & 0xFF, values[2] & 0xFF])
    sel2 = 0
    sizes = []
    for x in range(2, -1, -1):
        v = values[x]
        sel2 <<= 2
        if -128 <= v < 128:
            s = 0
        elif -32768 <= v < 32768:
            s = 1
        elif -8388608 <= v < 8388608:
            s = 2
        else:
            s = 3
        sel2 |= s
        sizes.insert(0, s)
    out = bytearray([(3 << 6) | sel2])
    for v, s in zip(values, sizes):
        out.extend((v & 0xFFFFFFFF).to_bytes(4, "little")[: s + 1])
    return bytes(out)


def _tag8_8svb(values: list[int]) -> bytes:
    if len(values) == 1:
        return _s_vb(values[0])
    header = 0
    for i in range(len(values) - 1, -1, -1):
        header <<= 1
        if values[i] != 0:
            header |= 1
    out = bytearray([header])
    for v in values:
        if v != 0:
            out.extend(_s_vb(v))
    return bytes(out)


@dataclass
class FieldSpec:
    name: str
    signed: int
    i_pred: int
    i_enc: int
    p_pred: int
    p_enc: int


def betaflight_main_fields(motors: int = 4, gyro_unfilt: bool = True, erpm: bool = True,
                           debug: bool = True) -> list[FieldSpec]:
    f: list[FieldSpec] = [
        FieldSpec("loopIteration", 0, P.P_ZERO, P.E_UNSIGNED_VB, P.P_INC, P.E_NULL),
        FieldSpec("time", 0, P.P_ZERO, P.E_UNSIGNED_VB, P.P_STRAIGHT_LINE, P.E_SIGNED_VB),
    ]
    for k in range(3):
        f.append(FieldSpec(f"axisP[{k}]", 1, P.P_ZERO, P.E_SIGNED_VB, P.P_PREVIOUS, P.E_SIGNED_VB))
    for k in range(3):
        f.append(FieldSpec(f"axisI[{k}]", 1, P.P_ZERO, P.E_SIGNED_VB, P.P_PREVIOUS, P.E_TAG2_3S32))
    for k in range(2):
        f.append(FieldSpec(f"axisD[{k}]", 1, P.P_ZERO, P.E_SIGNED_VB, P.P_PREVIOUS, P.E_SIGNED_VB))
    for k in range(3):
        f.append(FieldSpec(f"axisF[{k}]", 1, P.P_ZERO, P.E_SIGNED_VB, P.P_PREVIOUS, P.E_SIGNED_VB))
    for k in range(4):
        f.append(FieldSpec(f"rcCommand[{k}]", 1 if k < 3 else 0, P.P_ZERO,
                           P.E_SIGNED_VB if k < 3 else P.E_UNSIGNED_VB, P.P_PREVIOUS, P.E_TAG8_4S16))
    for k in range(4):
        f.append(FieldSpec(f"setpoint[{k}]", 1, P.P_ZERO, P.E_SIGNED_VB, P.P_PREVIOUS, P.E_TAG8_4S16))
    f.append(FieldSpec("vbatLatest", 0, P.P_VBATREF, P.E_NEG_14BIT, P.P_PREVIOUS, P.E_TAG8_8SVB))
    f.append(FieldSpec("amperageLatest", 1, P.P_ZERO, P.E_SIGNED_VB, P.P_PREVIOUS, P.E_TAG8_8SVB))
    f.append(FieldSpec("rssi", 0, P.P_ZERO, P.E_UNSIGNED_VB, P.P_PREVIOUS, P.E_TAG8_8SVB))
    for k in range(3):
        f.append(FieldSpec(f"gyroADC[{k}]", 1, P.P_ZERO, P.E_SIGNED_VB, P.P_AVERAGE_2, P.E_SIGNED_VB))
    if gyro_unfilt:
        for k in range(3):
            f.append(FieldSpec(f"gyroUnfilt[{k}]", 1, P.P_ZERO, P.E_SIGNED_VB, P.P_AVERAGE_2, P.E_SIGNED_VB))
    for k in range(3):
        f.append(FieldSpec(f"accSmooth[{k}]", 1, P.P_ZERO, P.E_SIGNED_VB, P.P_AVERAGE_2, P.E_SIGNED_VB))
    if debug:
        for k in range(8):
            f.append(FieldSpec(f"debug[{k}]", 1, P.P_ZERO, P.E_SIGNED_VB, P.P_AVERAGE_2, P.E_SIGNED_VB))
    for k in range(motors):
        if k == 0:
            f.append(FieldSpec("motor[0]", 0, P.P_MINMOTOR, P.E_UNSIGNED_VB, P.P_AVERAGE_2, P.E_SIGNED_VB))
        else:
            f.append(FieldSpec(f"motor[{k}]", 0, P.P_MOTOR_0, P.E_SIGNED_VB, P.P_AVERAGE_2, P.E_SIGNED_VB))
    if erpm:
        for k in range(motors):
            f.append(FieldSpec(f"eRPM[{k}]", 0, P.P_ZERO, P.E_UNSIGNED_VB, P.P_PREVIOUS, P.E_SIGNED_VB))
    return f


SLOW_FIELDS = [
    FieldSpec("flightModeFlags", 0, P.P_ZERO, P.E_UNSIGNED_VB, P.P_ZERO, P.E_UNSIGNED_VB),
    FieldSpec("stateFlags", 0, P.P_ZERO, P.E_UNSIGNED_VB, P.P_ZERO, P.E_UNSIGNED_VB),
    FieldSpec("failsafePhase", 0, P.P_ZERO, P.E_UNSIGNED_VB, P.P_ZERO, P.E_UNSIGNED_VB),
    FieldSpec("rxSignalReceived", 0, P.P_ZERO, P.E_UNSIGNED_VB, P.P_ZERO, P.E_UNSIGNED_VB),
    FieldSpec("rxFlightChannelsValid", 0, P.P_ZERO, P.E_UNSIGNED_VB, P.P_ZERO, P.E_UNSIGNED_VB),
]


class BlackboxWriter:
    def __init__(self, fields: list[FieldSpec], headers: dict[str, str],
                 i_interval: int = 32, minmotor: int = 48, vbatref: int = 420, p_interval: int = 1):
        self.fields = fields
        self.headers = headers
        self.i_interval = i_interval
        self.p_interval = p_interval
        self.minmotor = minmotor
        self.vbatref = vbatref
        self.out = bytearray()
        self.prev = None
        self.prev2 = None
        self.frame_index = 0
        self.m0 = [f.name for f in fields].index("motor[0]") if any(f.name == "motor[0]" for f in fields) else None

    def write_header(self) -> None:
        lines = [
            "Product:Blackbox flight data recorder by Nicholas Sherlock",
            "Data version:2",
            f"I interval:{self.i_interval}",
            f"P interval:{self.p_interval}",
            f"P ratio:{self.i_interval}",
            "Field I name:" + ",".join(f.name for f in self.fields),
            "Field I signed:" + ",".join(str(f.signed) for f in self.fields),
            "Field I predictor:" + ",".join(str(f.i_pred) for f in self.fields),
            "Field I encoding:" + ",".join(str(f.i_enc) for f in self.fields),
            "Field P predictor:" + ",".join(str(f.p_pred) for f in self.fields),
            "Field P encoding:" + ",".join(str(f.p_enc) for f in self.fields),
            "Field S name:" + ",".join(f.name for f in SLOW_FIELDS),
            "Field S signed:" + ",".join("0" for _ in SLOW_FIELDS),
            "Field S predictor:" + ",".join("0" for _ in SLOW_FIELDS),
            "Field S encoding:" + ",".join("1" for _ in SLOW_FIELDS),
        ]
        hdr = {"motorOutput": f"{self.minmotor},2047", "vbatref": str(self.vbatref)}
        hdr.update(self.headers)
        lines += [f"{k}:{v}" for k, v in hdr.items()]
        for line in lines:
            self.out.extend(b"H " + line.encode("latin-1") + b"\n")

    def _encode(self, residuals: list[int], encodings: list[int]) -> bytes:
        out = bytearray()
        i = 0
        n = len(residuals)
        while i < n:
            e = encodings[i]
            if e == P.E_SIGNED_VB:
                out += _s_vb(residuals[i])
                i += 1
            elif e == P.E_UNSIGNED_VB:
                out += _u_vb(residuals[i])
                i += 1
            elif e == P.E_NEG_14BIT:
                out += _u_vb((-residuals[i]) & 0x3FFF)
                i += 1
            elif e == P.E_NULL:
                i += 1
            elif e == P.E_TAG8_4S16:
                vals = residuals[i:i + 4] + [0] * (4 - len(residuals[i:i + 4]))
                out += _tag8_4s16(vals)
                i += 4
            elif e == P.E_TAG2_3S32:
                out += _tag2_3s32(residuals[i:i + 3])
                i += 3
            elif e == P.E_TAG8_8SVB:
                j = i + 1
                while j < i + 8 and j < n and encodings[j] == P.E_TAG8_8SVB:
                    j += 1
                out += _tag8_8svb(residuals[i:j])
                i = j
            else:
                raise ValueError(f"encoding {e} not supported by writer")
        return bytes(out)

    def write_main(self, values: list[int]) -> None:
        intra = self.frame_index % self.i_interval == 0 or self.prev is None
        res = []
        for k, (f, v) in enumerate(zip(self.fields, values)):
            pred = f.i_pred if intra else f.p_pred
            if pred == P.P_ZERO:
                p = 0
            elif pred == P.P_PREVIOUS:
                p = self.prev[k]
            elif pred == P.P_STRAIGHT_LINE:
                p = 2 * self.prev[k] - self.prev2[k]
            elif pred == P.P_AVERAGE_2:
                p = int((self.prev[k] + self.prev2[k]) / 2)
            elif pred == P.P_MINMOTOR:
                p = self.minmotor
            elif pred == P.P_MOTOR_0:
                p = values[self.m0]
            elif pred == P.P_VBATREF:
                p = self.vbatref
            elif pred == P.P_INC:
                p = v
            else:
                raise ValueError(pred)
            res.append(v - p)
        enc = [f.i_enc if intra else f.p_enc for f in self.fields]
        self.out += (b"I" if intra else b"P") + self._encode(res, enc)
        if intra:
            self.prev = self.prev2 = list(values)
        else:
            self.prev2, self.prev = self.prev, list(values)
        self.frame_index += 1

    def write_slow(self, values: list[int]) -> None:
        self.out += b"S" + b"".join(_u_vb(v) for v in values)

    def write_event_log_end(self) -> None:
        self.out += b"E" + bytes([P.EV_LOG_END]) + b"End of log\x00"

    def write_event_disarm(self, reason: int = 4) -> None:
        self.out += b"E" + bytes([P.EV_DISARM]) + _u_vb(reason)

    def getvalue(self) -> bytes:
        return bytes(self.out)

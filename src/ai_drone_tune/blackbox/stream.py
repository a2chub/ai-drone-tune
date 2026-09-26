"""Low level readers for the Betaflight blackbox binary encodings.

Mirrors ``blackbox_encoding.c`` in the firmware and ``decoders.js`` in the
blackbox-log-viewer.
"""

from __future__ import annotations


class StreamEOF(Exception):
    pass


def sign_extend(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    value &= (1 << bits) - 1
    return value - (1 << bits) if value & sign else value


class ByteStream:
    __slots__ = ("data", "pos", "end")

    def __init__(self, data: bytes, pos: int = 0, end: int | None = None):
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else end

    def read_byte(self) -> int:
        if self.pos >= self.end:
            raise StreamEOF
        b = self.data[self.pos]
        self.pos += 1
        return b

    def read_bytes(self, n: int) -> bytes:
        if self.pos + n > self.end:
            raise StreamEOF
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def read_unsigned_vb(self) -> int:
        data, pos, end = self.data, self.pos, self.end
        result = 0
        shift = 0
        for _ in range(5):
            if pos >= end:
                self.pos = pos
                raise StreamEOF
            b = data[pos]
            pos += 1
            result |= (b & 0x7F) << shift
            if b < 0x80:
                self.pos = pos
                return result
            shift += 7
        # Too many bytes: corrupt stream
        self.pos = pos
        return 0

    def read_signed_vb(self) -> int:
        u = self.read_unsigned_vb()
        return (u >> 1) ^ -(u & 1)

    def read_u32(self) -> int:
        b = self.read_bytes(4)
        return b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)

    # ------------------------------------------------------------------
    def read_tag2_3s32(self) -> list[int]:
        lead = self.read_byte()
        sel = lead >> 6
        if sel == 0:
            return [sign_extend(lead >> 4, 2), sign_extend(lead >> 2, 2), sign_extend(lead, 2)]
        if sel == 1:
            v0 = sign_extend(lead & 0x0F, 4)
            b = self.read_byte()
            return [v0, sign_extend(b >> 4, 4), sign_extend(b & 0x0F, 4)]
        if sel == 2:
            v0 = sign_extend(lead & 0x3F, 6)
            v1 = sign_extend(self.read_byte() & 0x3F, 6)
            v2 = sign_extend(self.read_byte() & 0x3F, 6)
            return [v0, v1, v2]
        return self._read_bytes_selector(lead)

    def _read_bytes_selector(self, lead: int) -> list[int]:
        values = [0, 0, 0]
        for i in range(3):
            kind = lead & 0x03
            if kind == 0:
                values[i] = sign_extend(self.read_byte(), 8)
            elif kind == 1:
                b = self.read_bytes(2)
                values[i] = sign_extend(b[0] | (b[1] << 8), 16)
            elif kind == 2:
                b = self.read_bytes(3)
                values[i] = sign_extend(b[0] | (b[1] << 8) | (b[2] << 16), 24)
            else:
                b = self.read_bytes(4)
                values[i] = sign_extend(b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24), 32)
            lead >>= 2
        return values

    def read_tag2_3svariable(self) -> list[int]:
        """Decoder matching ``blackboxWriteTag2_3SVariable`` in the firmware."""
        lead = self.read_byte()
        sel = lead >> 6
        if sel == 0:
            return [sign_extend(lead >> 4, 2), sign_extend(lead >> 2, 2), sign_extend(lead, 2)]
        if sel == 1:
            # 554: ss11 1112 | 2222 3333
            b = self.read_byte()
            v0 = sign_extend((lead >> 1) & 0x1F, 5)
            v1 = sign_extend(((lead & 0x01) << 4) | (b >> 4), 5)
            v2 = sign_extend(b & 0x0F, 4)
            return [v0, v1, v2]
        if sel == 2:
            # 877: ss11 1111 | 1122 2222 | 2333 3333
            b1 = self.read_byte()
            b2 = self.read_byte()
            v0 = sign_extend(((lead & 0x3F) << 2) | (b1 >> 6), 8)
            v1 = sign_extend(((b1 & 0x3F) << 1) | (b2 >> 7), 7)
            v2 = sign_extend(b2 & 0x7F, 7)
            return [v0, v1, v2]
        return self._read_bytes_selector(lead)

    def read_tag8_4s16_v2(self) -> list[int]:
        selector = self.read_byte()
        values = [0, 0, 0, 0]
        nibble = 0
        buffer = 0
        for i in range(4):
            kind = selector & 0x03
            if kind == 1:  # 4 bit
                if nibble == 0:
                    buffer = self.read_byte()
                    values[i] = sign_extend(buffer >> 4, 4)
                    nibble = 1
                else:
                    values[i] = sign_extend(buffer & 0x0F, 4)
                    nibble = 0
            elif kind == 2:  # 8 bit
                if nibble == 0:
                    values[i] = sign_extend(self.read_byte(), 8)
                else:
                    c1 = (buffer & 0x0F) << 4
                    buffer = self.read_byte()
                    values[i] = sign_extend(c1 | (buffer >> 4), 8)
            elif kind == 3:  # 16 bit
                if nibble == 0:
                    c1 = self.read_byte()
                    c2 = self.read_byte()
                    values[i] = sign_extend((c1 << 8) | c2, 16)
                else:
                    c1 = self.read_byte()
                    c2 = self.read_byte()
                    values[i] = sign_extend(((buffer & 0x0F) << 12) | (c1 << 4) | (c2 >> 4), 16)
                    buffer = c2
            selector >>= 2
        return values

    def read_tag8_4s16_v1(self) -> list[int]:
        selector = self.read_byte()
        values = [0, 0, 0, 0]
        i = 0
        while i < 4:
            kind = selector & 0x03
            if kind == 1:
                combined = self.read_byte()
                values[i] = sign_extend(combined & 0x0F, 4)
                i += 1
                selector >>= 2
                if i < 4:
                    values[i] = sign_extend(combined >> 4, 4)
            elif kind == 2:
                values[i] = sign_extend(self.read_byte(), 8)
            elif kind == 3:
                c1 = self.read_byte()
                c2 = self.read_byte()
                values[i] = sign_extend(c1 | (c2 << 8), 16)
            selector >>= 2
            i += 1
        return values

    def read_tag8_8svb(self, count: int) -> list[int]:
        if count == 1:
            return [self.read_signed_vb()]
        header = self.read_byte()
        values = [0] * count
        for i in range(count):
            if header & 0x01:
                values[i] = self.read_signed_vb()
            header >>= 1
        return values

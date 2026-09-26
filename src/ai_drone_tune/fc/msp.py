"""MultiWii Serial Protocol (MSP v1 / v2) codec and client.

Only the subset of commands needed for identification and blackbox dataflash
handling is wrapped here; settings are changed through the CLI (see
``cli_session.py``) because every Betaflight setting is reachable that way.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

# --- MSP command codes (betaflight/src/main/msp/msp_protocol*.h) -----------
MSP_API_VERSION = 1
MSP_FC_VARIANT = 2
MSP_FC_VERSION = 3
MSP_BOARD_INFO = 4
MSP_BUILD_INFO = 5
MSP_NAME = 10
MSP_REBOOT = 68
MSP_DATAFLASH_SUMMARY = 70
MSP_DATAFLASH_READ = 71
MSP_DATAFLASH_ERASE = 72
MSP_SDCARD_SUMMARY = 79
MSP_BLACKBOX_CONFIG = 80
MSP_STATUS = 101
MSP2_GET_TEXT = 0x3006

MSP2TEXT_PILOT_NAME = 1
MSP2TEXT_CRAFT_NAME = 2

# MSP_REBOOT types
REBOOT_FIRMWARE = 0
REBOOT_BOOTLOADER_ROM = 1
REBOOT_MSC = 2
REBOOT_MSC_UTC = 3

FLASHFS_FLAG_READY = 1
FLASHFS_FLAG_SUPPORTED = 2


class MSPError(Exception):
    pass


def crc8_dvb_s2(crc: int, byte: int) -> int:
    crc ^= byte
    for _ in range(8):
        crc = ((crc << 1) ^ 0xD5) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def encode_v1(cmd: int, payload: bytes = b"") -> bytes:
    if cmd > 254:
        raise ValueError("MSP v1 command must be < 255, use v2")
    size = len(payload)
    if size >= 255:
        raise ValueError("MSP v1 payload too large")
    checksum = size ^ cmd
    for b in payload:
        checksum ^= b
    return b"$M<" + bytes([size, cmd]) + payload + bytes([checksum])


def encode_v2(cmd: int, payload: bytes = b"", flags: int = 0) -> bytes:
    header = struct.pack("<BHH", flags, cmd, len(payload))
    crc = 0
    for b in header + payload:
        crc = crc8_dvb_s2(crc, b)
    return b"$X<" + header + payload + bytes([crc])


@dataclass
class MSPResponse:
    cmd: int
    payload: bytes
    error: bool = False
    version: int = 1


class MSPDecoder:
    """Incremental decoder for MSP responses (v1, v1 jumbo and v2)."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[MSPResponse]:
        self.buf.extend(data)
        out: list[MSPResponse] = []
        while True:
            resp = self._try_parse()
            if resp is None:
                break
            out.append(resp)
        return out

    def _try_parse(self) -> MSPResponse | None:
        buf = self.buf
        start = buf.find(b"$")
        if start < 0:
            buf.clear()
            return None
        if start:
            del buf[:start]
        if len(buf) < 3:
            return None
        proto, direction = buf[1], buf[2]
        if proto not in (ord("M"), ord("X")) or direction not in (ord(">"), ord("!"), ord("<")):
            del buf[:1]
            return None
        error = direction == ord("!")
        if proto == ord("M"):
            if len(buf) < 5:
                return None
            size, cmd = buf[3], buf[4]
            hdr = 5
            if size == 255:  # jumbo frame
                if len(buf) < 7:
                    return None
                size = buf[5] | (buf[6] << 8)
                hdr = 7
            total = hdr + size + 1
            if len(buf) < total:
                return None
            payload = bytes(buf[hdr:hdr + size])
            checksum = 0
            for b in buf[3:hdr + size]:
                checksum ^= b
            ok = checksum == buf[hdr + size]
            del buf[:total]
            if not ok:
                return None if not self.buf else self._try_parse()
            if direction == ord("<"):  # our own echo; ignore
                return self._try_parse()
            return MSPResponse(cmd, payload, error, 1)
        # MSP v2
        if len(buf) < 8:
            return None
        _flags, cmd, size = struct.unpack_from("<BHH", buf, 3)
        total = 8 + size + 1
        if len(buf) < total:
            return None
        crc = 0
        for b in buf[3:8 + size]:
            crc = crc8_dvb_s2(crc, b)
        ok = crc == buf[8 + size]
        payload = bytes(buf[8:8 + size])
        del buf[:total]
        if not ok or direction == ord("<"):
            return self._try_parse()
        return MSPResponse(cmd, payload, error, 2)


@dataclass
class DataflashSummary:
    ready: bool
    supported: bool
    sectors: int
    total_size: int
    used_size: int


@dataclass
class SdcardSummary:
    supported: bool
    state: int
    last_error: int
    free_kb: int
    total_kb: int


class MSPClient:
    """Request/response MSP client on top of a pyserial-like port object."""

    def __init__(self, port, timeout: float = 1.0, use_v2: bool = True):
        self.port = port
        self.timeout = timeout
        self.use_v2 = use_v2
        self.decoder = MSPDecoder()

    def request(self, cmd: int, payload: bytes = b"", timeout: float | None = None,
                retries: int = 2) -> bytes:
        last_err: Exception | None = None
        for _ in range(retries + 1):
            try:
                return self._request_once(cmd, payload, timeout or self.timeout)
            except MSPError as e:
                last_err = e
                self.decoder = MSPDecoder()
                try:
                    self.port.reset_input_buffer()
                except Exception:
                    pass
        raise MSPError(f"MSP command {cmd} failed: {last_err}")

    def _request_once(self, cmd: int, payload: bytes, timeout: float) -> bytes:
        v2 = self.use_v2 or cmd > 254 or len(payload) >= 255
        frame = encode_v2(cmd, payload) if v2 else encode_v1(cmd, payload)
        self.port.write(frame)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            waiting = getattr(self.port, "in_waiting", 0) or 1
            data = self.port.read(waiting)
            if not data:
                continue
            for resp in self.decoder.feed(data):
                if resp.cmd != cmd:
                    continue
                if resp.error:
                    raise MSPError(f"FC returned error for MSP command {cmd}")
                return resp.payload
        raise MSPError(f"timeout waiting for MSP command {cmd}")

    # ------------------------------------------------------------------
    # High level helpers
    # ------------------------------------------------------------------
    def api_version(self) -> tuple[int, int]:
        """Returns (api_major, api_minor). Byte 0 is the MSP protocol version."""
        p = self.request(MSP_API_VERSION)
        return p[1], p[2]

    def fc_variant(self) -> str:
        return self.request(MSP_FC_VARIANT)[:4].decode("ascii", "replace")

    def fc_version(self) -> str:
        p = self.request(MSP_FC_VERSION)
        return f"{p[0]}.{p[1]}.{p[2]}"

    def board_info(self) -> str:
        p = self.request(MSP_BOARD_INFO)
        return p[:4].decode("ascii", "replace").strip("\x00")

    def craft_name(self) -> str:
        try:
            p = self.request(MSP2_GET_TEXT, bytes([MSP2TEXT_CRAFT_NAME]), retries=0)
            if len(p) >= 2:
                length = p[1]
                return p[2:2 + length].decode("utf-8", "replace")
        except MSPError:
            pass
        try:
            return self.request(MSP_NAME, retries=0).decode("utf-8", "replace").strip("\x00")
        except MSPError:
            return ""

    def dataflash_summary(self) -> DataflashSummary:
        p = self.request(MSP_DATAFLASH_SUMMARY)
        if len(p) < 13:
            return DataflashSummary(False, False, 0, 0, 0)
        flags, sectors, total, used = struct.unpack_from("<BIII", p, 0)
        return DataflashSummary(bool(flags & FLASHFS_FLAG_READY), bool(flags & FLASHFS_FLAG_SUPPORTED),
                                sectors, total, used)

    def sdcard_summary(self) -> SdcardSummary:
        p = self.request(MSP_SDCARD_SUMMARY)
        if len(p) < 11:
            return SdcardSummary(False, 0, 0, 0, 0)
        flags, state, err, free_kb, total_kb = struct.unpack_from("<BBBII", p, 0)
        return SdcardSummary(bool(flags & 1), state, err, free_kb, total_kb)

    def dataflash_read(self, address: int, size: int) -> tuple[int, bytes]:
        """Read a chunk of dataflash. Returns (address, data). Data may be shorter than requested."""
        payload = struct.pack("<IHB", address, size, 0)  # 0 = no compression
        p = self.request(MSP_DATAFLASH_READ, payload, timeout=max(self.timeout, 2.0))
        if len(p) < 4:
            raise MSPError("short dataflash read reply")
        chunk_addr = struct.unpack_from("<I", p, 0)[0]
        if len(p) >= 7:
            data_size, compression = struct.unpack_from("<HB", p, 4)
            if compression != 0:
                raise MSPError("compressed dataflash reply not supported")
            data = p[7:7 + data_size]
            if len(data) != data_size:
                raise MSPError("truncated dataflash reply")
        else:
            data = p[4:]
        return chunk_addr, bytes(data)

    def dataflash_erase(self) -> None:
        self.request(MSP_DATAFLASH_ERASE, timeout=3.0)

    def reboot(self, reboot_type: int = REBOOT_FIRMWARE) -> None:
        frame = encode_v2(MSP_REBOOT, bytes([reboot_type])) if self.use_v2 else encode_v1(MSP_REBOOT, bytes([reboot_type]))
        self.port.write(frame)

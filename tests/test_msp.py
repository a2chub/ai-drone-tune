import struct

from ai_drone_tune.fc import msp as M
from ai_drone_tune.fc.emulator import EmulatedFC


def test_v1_and_v2_decode():
    dec = M.MSPDecoder()
    frame_v1 = b"$M>" + bytes([3, 1, 0, 1, 46]) + bytes([3 ^ 1 ^ 0 ^ 1 ^ 46])
    payload = b"x" * 300
    hdr = struct.pack("<BHH", 0, 71, len(payload))
    crc = 0
    for b in hdr + payload:
        crc = M.crc8_dvb_s2(crc, b)
    frame_v2 = b"$X>" + hdr + payload + bytes([crc])
    out = dec.feed(b"garbage" + frame_v1[:4]) + dec.feed(frame_v1[4:] + frame_v2)
    assert [(r.cmd, r.version) for r in out] == [(1, 1), (71, 2)]
    assert out[1].payload == payload


def test_bad_checksum_is_dropped():
    dec = M.MSPDecoder()
    frame = bytearray(b"$M>" + bytes([1, 2, 7]) + bytes([1 ^ 2 ^ 7]))
    frame[-1] ^= 0xFF
    assert dec.feed(bytes(frame)) == []


def test_client_against_emulator():
    emu = EmulatedFC(flash=b"H Product:Blackbox flight data recorder by Nicholas Sherlock\n" + bytes(range(256)) * 20,
                     craft_name="QUAD")
    c = M.MSPClient(emu, use_v2=False)
    assert c.api_version() == (1, 46)
    c.use_v2 = True
    assert c.fc_variant() == "BTFL"
    assert c.fc_version() == "4.5.1"
    assert c.craft_name() == "QUAD"
    s = c.dataflash_summary()
    assert s.supported and s.ready and s.used_size == len(emu.flash)
    addr, data = c.dataflash_read(100, 4096)
    assert addr == 100 and data == bytes(emu.flash[100:100 + emu.max_chunk])


def test_jumbo_v1_frame():
    emu = EmulatedFC(flash=bytes(1000))
    c = M.MSPClient(emu, use_v2=False)
    addr, data = c.dataflash_read(0, 400)
    assert len(data) == 400

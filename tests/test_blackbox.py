import random

import numpy as np

from ai_drone_tune.blackbox import parser as P
from ai_drone_tune.blackbox import writer as W
from ai_drone_tune.blackbox.stream import ByteStream


def _random_rows(fields, n, seed=1):
    rnd = random.Random(seed)
    rows = []
    for k in range(n):
        row = []
        for f in fields:
            if f.name == "loopIteration":
                v = k
            elif f.name == "time":
                v = 1_000_000 + k * 500 + rnd.randint(-3, 3)
            elif f.name.startswith("motor"):
                v = rnd.randint(0, 2047)  # includes values below minmotor (negative residual)
            elif f.name == "vbatLatest":
                v = rnd.randint(300, 420)
            elif f.name.startswith(("rcCommand", "setpoint")):
                v = rnd.randint(0, 1000) if f.name.endswith("[3]") else rnd.randint(-1000, 1000)
            elif f.signed:
                v = rnd.randint(-40000, 40000) if rnd.random() < 0.3 else rnd.randint(-5, 5)
            else:
                v = rnd.randint(0, 5000)
            row.append(v)
        rows.append(row)
    return rows


def _write(rows, fields, headers=None):
    w = W.BlackboxWriter(fields, headers or {"Firmware revision": "Betaflight 4.5.1 (abc) STM32F7X2",
                                             "Craft name": "TEST"})
    w.write_header()
    for k, r in enumerate(rows):
        w.write_main(r)
        if k % 100 == 0:
            w.write_slow([1, 0, 0, 1, 1])
    w.write_event_disarm(4)
    w.write_event_log_end()
    return w.getvalue()


def test_roundtrip_exact():
    fields = W.betaflight_main_fields()
    rows = _random_rows(fields, 3000)
    logs = P.parse_bytes(_write(rows, fields))
    assert len(logs) == 1
    lg = logs[0]
    assert np.array_equal(lg.frames, np.array(rows))
    assert lg.stats["corrupt"] == 0
    assert lg.craft_name == "TEST"
    assert lg.firmware_version == (4, 5, 1)
    assert lg.slow_frames is not None and lg.slow_frames.shape[0] == 30
    assert [e.type for e in lg.events] == [P.EV_DISARM, P.EV_LOG_END]
    assert lg.axis("gyroADC").shape == (3000, 3)


def test_multiple_logs_in_one_file():
    fields = W.betaflight_main_fields(gyro_unfilt=False, erpm=False)
    a = _write(_random_rows(fields, 500, seed=1), fields)
    b = _write(_random_rows(fields, 700, seed=2), fields)
    logs = P.parse_bytes(a + b)
    assert [len(lg.frames) for lg in logs] == [500, 700]
    assert [lg.index for lg in logs] == [0, 1]


def test_corruption_resyncs_at_next_iframe():
    fields = W.betaflight_main_fields()
    rows = _random_rows(fields, 2000)
    data = bytearray(_write(rows, fields))
    header_end = data.index(b"\nI") + 1
    # destroy a chunk in the middle of the data section
    mid = header_end + (len(data) - header_end) // 2
    data[mid:mid + 40] = b"\xff" * 40
    lg = P.parse_bytes(bytes(data))[0]
    assert lg.stats["corrupt"] > 0
    # most frames survive and the ones kept after the damage are still exact
    assert len(lg.frames) > 1800
    good = {r[0]: r for r in rows}
    last = lg.frames[-1]
    assert list(last) == good[int(last[0])]


def test_truncated_file_does_not_crash():
    fields = W.betaflight_main_fields()
    data = _write(_random_rows(fields, 1000), fields)
    lg = P.parse_bytes(data[: len(data) * 2 // 3])[0]
    assert 500 < len(lg.frames) < 1000


def test_tag2_3svariable_matches_firmware_encoder():
    def fw_encode(v):  # blackboxWriteTag2_3SVariable (877 / 554 branches)
        if (v[0] >= 256 or v[0] < -256 or v[1] >= 128 or v[1] < -128 or v[2] >= 128 or v[2] < -128):
            raise ValueError
        if v[0] >= 16 or v[0] < -16 or v[1] >= 16 or v[1] < -16 or v[2] >= 8 or v[2] < -8:
            return bytes([(2 << 6) | ((v[0] & 0xFF) >> 2), ((v[0] & 0x03) << 6) | ((v[1] & 0x7F) >> 1),
                          ((v[1] & 0x01) << 7) | (v[2] & 0x7F)])
        if any(x >= 2 or x < -2 for x in v):
            return bytes([(1 << 6) | ((v[0] & 0x1F) << 1) | ((v[1] & 0x1F) >> 4),
                          ((v[1] & 0x0F) << 4) | (v[2] & 0x0F)])
        return bytes([((v[0] & 3) << 4) | ((v[1] & 3) << 2) | (v[2] & 3)])

    rnd = random.Random(5)
    for _ in range(2000):
        v = [rnd.randint(-120, 120), rnd.randint(-60, 60), rnd.randint(-60, 60)]
        if rnd.random() < 0.5:
            v = [rnd.randint(-15, 15), rnd.randint(-15, 15), rnd.randint(-7, 7)]
        assert ByteStream(fw_encode(v)).read_tag2_3svariable() == v


def test_tag8_4s16_roundtrip():
    rnd = random.Random(7)
    for _ in range(2000):
        vals = [rnd.choice([0, rnd.randint(-8, 7), rnd.randint(-128, 127), rnd.randint(-32768, 32767)])
                for _ in range(4)]
        assert ByteStream(W._tag8_4s16(vals)).read_tag8_4s16_v2() == vals


def test_simulated_log_parses(sim_log_bytes):
    lg = P.parse_bytes(sim_log_bytes)[0]
    assert lg.stats["corrupt"] == 0
    assert 1900 < lg.sample_rate_hz < 2100
    assert "gyroUnfilt[0]" in lg and "eRPM[3]" in lg

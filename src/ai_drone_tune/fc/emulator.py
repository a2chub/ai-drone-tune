"""In-memory Betaflight flight controller emulator (MSP + CLI).

Implements the serial behaviour this tool relies on so the whole pipeline
can be tested and demonstrated without hardware (``aidt demo``).
"""

from __future__ import annotations

import struct

from . import msp as M

DEFAULT_SETTINGS: list[tuple[str, str, str, str]] = [
    # name, value, section, range ("lo-hi" or "A|B|C")
    ("craft_name", "", "master", "str"),
    ("gyro_lpf1_type", "PT1", "master", "PT1|BIQUAD|PT2|PT3"),
    ("gyro_lpf1_static_hz", "250", "master", "0-1000"),
    ("gyro_lpf1_dyn_min_hz", "250", "master", "0-1000"),
    ("gyro_lpf1_dyn_max_hz", "500", "master", "0-1000"),
    ("gyro_lpf2_type", "PT1", "master", "PT1|BIQUAD|PT2|PT3"),
    ("gyro_lpf2_static_hz", "500", "master", "0-1000"),
    ("dyn_notch_count", "3", "master", "0-7"),
    ("dyn_notch_q", "300", "master", "1-1000"),
    ("dyn_notch_min_hz", "100", "master", "20-250"),
    ("dyn_notch_max_hz", "600", "master", "200-2000"),
    ("dshot_bidir", "ON", "master", "OFF|ON"),
    ("motor_poles", "14", "master", "4-255"),
    ("rpm_filter_harmonics", "3", "master", "0-3"),
    ("rpm_filter_q", "500", "master", "250-3000"),
    ("rpm_filter_min_hz", "100", "master", "30-200"),
    ("debug_mode", "NONE", "master", "NONE|CYCLETIME|BATTERY|GYRO_FILTERED|ACCELEROMETER|PIDLOOP|GYRO_SCALED"),
    ("blackbox_sample_rate", "1/2", "master", "1/1|1/2|1/4|1/8|1/16"),
    ("blackbox_device", "SPIFLASH", "master", "NONE|SPIFLASH|SDCARD|SERIAL"),
    ("blackbox_mode", "NORMAL", "master", "NORMAL|MOTOR_TEST|ALWAYS"),
    ("blackbox_disable_gyrounfilt", "OFF", "master", "OFF|ON"),
    ("blackbox_disable_setpoint", "OFF", "master", "OFF|ON"),
    ("blackbox_disable_rpm", "OFF", "master", "OFF|ON"),
    ("blackbox_disable_motors", "OFF", "master", "OFF|ON"),
    ("blackbox_disable_pids", "OFF", "master", "OFF|ON"),
    ("blackbox_disable_debug", "OFF", "master", "OFF|ON"),
    ("pid_process_denom", "1", "master", "1-16"),
    ("p_roll", "45", "profile", "0-250"), ("i_roll", "80", "profile", "0-250"),
    ("d_roll", "40", "profile", "0-250"), ("f_roll", "120", "profile", "0-1000"),
    ("p_pitch", "47", "profile", "0-250"), ("i_pitch", "84", "profile", "0-250"),
    ("d_pitch", "46", "profile", "0-250"), ("f_pitch", "125", "profile", "0-1000"),
    ("p_yaw", "45", "profile", "0-250"), ("i_yaw", "80", "profile", "0-250"),
    ("d_yaw", "0", "profile", "0-250"), ("f_yaw", "120", "profile", "0-1000"),
    ("d_max_roll", "0", "profile", "0-250"), ("d_max_pitch", "0", "profile", "0-250"),
    ("d_max_yaw", "0", "profile", "0-250"),
    ("dterm_lpf1_type", "PT1", "profile", "PT1|BIQUAD|PT2|PT3"),
    ("dterm_lpf1_static_hz", "75", "profile", "0-1000"),
    ("dterm_lpf1_dyn_min_hz", "75", "profile", "0-1000"),
    ("dterm_lpf1_dyn_max_hz", "150", "profile", "0-1000"),
    ("dterm_lpf2_type", "PT1", "profile", "PT1|BIQUAD|PT2|PT3"),
    ("dterm_lpf2_static_hz", "150", "profile", "0-1000"),
    ("tpa_rate", "65", "profile", "0-100"), ("tpa_breakpoint", "1350", "profile", "750-2250"),
    ("dyn_idle_min_rpm", "0", "profile", "0-200"),
    ("iterm_relax_cutoff", "15", "profile", "1-50"),
    ("anti_gravity_gain", "80", "profile", "0-250"),
    ("simplified_pids_mode", "RPY", "profile", "OFF|RP|RPY"),
    ("simplified_gyro_filter", "ON", "master", "OFF|ON"),
    ("simplified_dterm_filter", "ON", "profile", "OFF|ON"),
    ("rates_type", "ACTUAL", "rateprofile", "BETAFLIGHT|RACEFLIGHT|KISS|ACTUAL|QUICK"),
    ("roll_rc_rate", "7", "rateprofile", "1-255"), ("pitch_rc_rate", "7", "rateprofile", "1-255"),
    ("yaw_rc_rate", "7", "rateprofile", "1-255"),
    ("roll_expo", "0", "rateprofile", "0-100"), ("pitch_expo", "0", "rateprofile", "0-100"),
    ("yaw_expo", "0", "rateprofile", "0-100"),
    ("roll_srate", "67", "rateprofile", "0-255"), ("pitch_srate", "67", "rateprofile", "0-255"),
    ("yaw_srate", "67", "rateprofile", "0-255"),
]


class EmulatedFC:
    """A fake serial port. ``write()`` requests, ``read()`` replies."""

    def __init__(self, flash: bytes = b"", craft_name: str = "EMU", version=(4, 5, 1), flash_size=16 * 1024 * 1024):
        self.flash = bytearray(flash)
        self.flash_size = flash_size
        self.version = version
        self.settings = {n: [v, s, r] for n, v, s, r in DEFAULT_SETTINGS}
        self.settings["craft_name"][0] = craft_name
        self.saved = dict((k, v[0]) for k, v in self.settings.items())
        self.out = bytearray()
        self.inbuf = bytearray()
        self.cli = False
        self.closed = False
        self.reboots = 0
        self.erasing = 0
        self.max_chunk = 480
        self.profile = 0
        self.rateprofile = 0
        self.cli_log: list[str] = []
        self.armed = False

    # --- pyserial-like API ---------------------------------------------
    @property
    def in_waiting(self) -> int:
        return len(self.out)

    def read(self, n: int = 1) -> bytes:
        n = max(1, n)
        data = bytes(self.out[:n])
        del self.out[:n]
        return data

    def write(self, data: bytes) -> int:
        if self.closed:
            raise OSError("port closed")
        self.inbuf.extend(data)
        self._process()
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.out.clear()

    def close(self):
        self.closed = True

    # --------------------------------------------------------------------
    def _process(self) -> None:
        while self.inbuf:
            if self.cli:
                nl = self.inbuf.find(b"\n")
                if nl < 0:
                    return
                line = self.inbuf[:nl].decode().strip()
                del self.inbuf[:nl + 1]
                self._cli_line(line)
                continue
            if self.inbuf[0:1] == b"#":
                del self.inbuf[:1]
                self.cli = True
                self.out += b"\r\nEntering CLI Mode, type 'exit' to return, or 'help'\r\n\r\n# "
                continue
            if self.inbuf[0:1] != b"$":
                del self.inbuf[:1]
                continue
            if len(self.inbuf) < 3:
                return
            if self.inbuf[1:3] == b"M<":
                if len(self.inbuf) < 6:
                    return
                size, cmd = self.inbuf[3], self.inbuf[4]
                if len(self.inbuf) < 6 + size:
                    return
                payload = bytes(self.inbuf[5:5 + size])
                del self.inbuf[:6 + size]
                self._reply(cmd, payload, v2=False)
            elif self.inbuf[1:3] == b"X<":
                if len(self.inbuf) < 9:
                    return
                _f, cmd, size = struct.unpack_from("<BHH", self.inbuf, 3)
                if len(self.inbuf) < 9 + size:
                    return
                payload = bytes(self.inbuf[8:8 + size])
                del self.inbuf[:9 + size]
                self._reply(cmd, payload, v2=True)
            else:
                del self.inbuf[:1]

    def _send(self, cmd: int, payload: bytes, v2: bool, error: bool = False) -> None:
        d = b"!" if error else b">"
        if v2:
            header = struct.pack("<BHH", 0, cmd, len(payload))
            crc = 0
            for b in header + payload:
                crc = M.crc8_dvb_s2(crc, b)
            self.out += b"$X" + d + header + payload + bytes([crc])
        else:
            if len(payload) >= 255:
                # jumbo frame: $M> 255 cmd lenLo lenHi payload checksum
                body = bytes([255, cmd, len(payload) & 0xFF, len(payload) >> 8]) + payload
                ck = 0
                for b in body:
                    ck ^= b
                self.out += b"$M" + d + body + bytes([ck])
            else:
                ck = len(payload) ^ cmd
                for b in payload:
                    ck ^= b
                self.out += b"$M" + d + bytes([len(payload), cmd]) + payload + bytes([ck])

    def _reply(self, cmd: int, p: bytes, v2: bool) -> None:
        if cmd == M.MSP_API_VERSION:
            self._send(cmd, bytes([0, 1, 46]), v2)
        elif cmd == M.MSP_FC_VARIANT:
            self._send(cmd, b"BTFL", v2)
        elif cmd == M.MSP_FC_VERSION:
            self._send(cmd, bytes(self.version), v2)
        elif cmd == M.MSP_BOARD_INFO:
            self._send(cmd, b"S7X2" + b"\x00" * 4, v2)
        elif cmd == M.MSP2_GET_TEXT:
            name = self.settings["craft_name"][0].encode()
            self._send(cmd, bytes([p[0] if p else 2, len(name)]) + name, v2)
        elif cmd == M.MSP_NAME:
            self._send(cmd, self.settings["craft_name"][0].encode(), v2)
        elif cmd == M.MSP_DATAFLASH_SUMMARY:
            ready = 0 if self.erasing > 0 else M.FLASHFS_FLAG_READY
            if self.erasing > 0:
                self.erasing -= 1
            self._send(cmd, struct.pack("<BIII", ready | M.FLASHFS_FLAG_SUPPORTED, 256, self.flash_size,
                                        len(self.flash)), v2)
        elif cmd == M.MSP_DATAFLASH_READ:
            addr, size = struct.unpack_from("<IH", p, 0)
            size = min(size, self.max_chunk)
            data = bytes(self.flash[addr:addr + size])
            self._send(cmd, struct.pack("<IHB", addr, len(data), 0) + data, v2)
        elif cmd == M.MSP_DATAFLASH_ERASE:
            self.flash = bytearray()
            self.erasing = 2
            self._send(cmd, b"", v2)
        elif cmd == M.MSP_STATUS:
            self._send(cmd, struct.pack("<HHHIBH", 250, 0, 0x21, 1 if self.armed else 0, self.profile, 120), v2)
        elif cmd == M.MSP_BLACKBOX_CONFIG:
            rate = M.BLACKBOX_SAMPLE_RATES.index(self.settings["blackbox_sample_rate"][0])
            self._send(cmd, struct.pack("<BBBBHBI", 1, 1, 1, 1 << rate, 32, rate, 0), v2)
        elif cmd == M.MSP_SDCARD_SUMMARY:
            self._send(cmd, struct.pack("<BBBII", 0, 0, 0, 0, 0), v2)
        else:
            self._send(cmd, b"", v2, error=True)

    # --------------------------------------------------------------------
    def _print(self, text: str) -> None:
        self.out += text.replace("\n", "\r\n").encode()

    def _prompt(self) -> None:
        self.out += b"\r\n# "

    def _var_block(self, name: str) -> str:
        value, section, rng = self.settings[name]
        lines = [f"{name} = {value}"]
        if section == "profile":
            lines.append(f"profile {self.profile}")
        elif section == "rateprofile":
            lines.append(f"rateprofile {self.rateprofile}")
        if rng == "str":
            lines.append("String length: 0 - 16")
        elif "|" in rng:
            lines.append("Allowed values: " + ", ".join(rng.split("|")))
        else:
            lo, hi = rng.split("-")
            lines.append(f"Allowed range: {lo} - {hi}")
        lines.append(f"Default value: {value}")
        return "\n".join(lines)

    def _cli_line(self, line: str) -> None:
        self.cli_log.append(line)
        self._print(line + "\n")  # echo
        if line in ("save", "exit"):
            if line == "save":
                self.saved = dict((k, v[0]) for k, v in self.settings.items())
                self._print("# saving\nRebooting")
            self.cli = False
            self.reboots += 1
            self.closed = True
            return
        if line == "get":
            self._print("\n\n".join(self._var_block(n) for n in self.settings))
        elif line.startswith("get "):
            q = line[4:].strip()
            matches = [n for n in self.settings if q.lower() in n]
            if not matches:
                self._print("###ERROR IN get: INVALID NAME###")
            self._print("\n\n".join(self._var_block(n) for n in matches))
        elif line.startswith("set "):
            body = line[4:]
            if "=" not in body:
                self._print("###ERROR IN set: INVALID NAME###")
            else:
                name, value = (x.strip() for x in body.split("=", 1))
                if name not in self.settings:
                    self._print("###ERROR IN set: INVALID NAME###")
                else:
                    rng = self.settings[name][2]
                    ok = True
                    if "|" in rng:
                        ok = value.upper() in rng.split("|")
                        value = value.upper()
                    elif rng != "str":
                        lo, hi = (int(x) for x in rng.split("-"))
                        ok = value.lstrip("-").isdigit() and lo <= int(value) <= hi
                    if ok:
                        self.settings[name][0] = value
                        self._print(f"{name} set to {value}")
                    else:
                        self._print("###ERROR IN set: INVALID VALUE###")
        elif line.startswith("profile"):
            parts = line.split()
            if len(parts) > 1:
                self.profile = int(parts[1])
            self._print(f"profile {self.profile}")
        elif line.startswith("rateprofile"):
            parts = line.split()
            if len(parts) > 1:
                self.rateprofile = int(parts[1])
            self._print(f"rateprofile {self.rateprofile}")
        elif line.startswith(("diff", "dump")):
            out = ["# version", "# Betaflight / STM32F7X2 (S7X2) %d.%d.%d" % self.version, "", "# master"]
            for n, (v, s, _r) in self.settings.items():
                if s == "master":
                    out.append(f"set {n} = {v}")
            out += ["", f"profile {self.profile}", ""]
            out += [f"set {n} = {v}" for n, (v, s, _r) in self.settings.items() if s == "profile"]
            out += ["", f"rateprofile {self.rateprofile}", ""]
            out += [f"set {n} = {v}" for n, (v, s, _r) in self.settings.items() if s == "rateprofile"]
            self._print("\n".join(out))
        else:
            self._print(f"###ERROR IN {line.split()[0] if line else ''}: UNKNOWN COMMAND###")
        self._prompt()

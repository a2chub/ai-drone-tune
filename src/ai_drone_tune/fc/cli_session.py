"""Betaflight CLI session over serial.

Entering CLI mode is done by sending ``#``. Every command response ends with
the ``\\r\\n# `` prompt. ``save`` and ``exit`` reboot the FC, closing the port.
"""

from __future__ import annotations

import time


PROMPT = b"\r\n# "


class CLIError(Exception):
    pass


class CLISession:
    def __init__(self, port, timeout: float = 3.0):
        self.port = port
        self.timeout = timeout
        self.active = False

    def enter(self) -> str:
        try:
            self.port.reset_input_buffer()
        except Exception:
            pass
        self.port.write(b"#")
        out = self._read_until_prompt(self.timeout)
        if "CLI" not in out and not out.rstrip().endswith("#"):
            raise CLIError(f"failed to enter CLI mode: {out!r}")
        self.active = True
        return out

    def _read_until_prompt(self, timeout: float) -> str:
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            waiting = getattr(self.port, "in_waiting", 0) or 1
            data = self.port.read(waiting)
            if data:
                buf.extend(data)
                # extend the deadline while data keeps streaming (e.g. `dump all`)
                deadline = max(deadline, time.monotonic() + 1.0)
                if buf.endswith(PROMPT) or buf.endswith(b"\n# "):
                    # comment lines in dump output ("# version") also start with
                    # "\r\n# "; make sure the stream really went quiet.
                    if not self._more_data_pending(0.15, buf):
                        break
        else:
            raise CLIError(f"timeout waiting for CLI prompt, got: {bytes(buf[-200:])!r}")
        return buf.decode("utf-8", "replace")

    def _more_data_pending(self, wait: float, buf: bytearray) -> bool:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            waiting = getattr(self.port, "in_waiting", 0)
            if waiting:
                buf.extend(self.port.read(waiting))
                return True
            time.sleep(0.01)
        return False

    def command(self, cmd: str, timeout: float | None = None) -> str:
        if not self.active:
            self.enter()
        self.port.write(cmd.encode("ascii") + b"\n")
        out = self._read_until_prompt(timeout or self.timeout)
        # strip echoed command and trailing prompt
        lines = out.replace("\r", "").split("\n")
        if lines and lines[0].strip() == cmd.strip():
            lines = lines[1:]
        while lines and lines[-1].strip() in ("#", ""):
            lines.pop()
        text = "\n".join(lines)
        if "###ERROR" in text:
            raise CLIError(text.strip())
        return text

    def set(self, name: str, value) -> str:
        return self.command(f"set {name} = {value}")

    def save(self) -> None:
        """Save and reboot. The serial port becomes invalid afterwards."""
        self.port.write(b"save\n")
        self.port.flush()
        time.sleep(0.5)
        self.active = False

    def exit(self) -> None:
        """Leave CLI without saving (FC reboots)."""
        self.port.write(b"exit\n")
        self.port.flush()
        time.sleep(0.3)
        self.active = False

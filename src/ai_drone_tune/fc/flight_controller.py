"""High level flight controller facade: identification, blackbox and CLI access."""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, asdict

from .cli_session import CLISession
from .msp import MSPClient, MSPError


@dataclass
class FCInfo:
    port: str
    variant: str = ""
    version: str = ""
    api_version: str = ""
    board: str = ""
    craft_name: str = ""
    armed: bool = False
    pid_loop_hz: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class FlightController:
    def __init__(self, device: str, baudrate: int = 115200, timeout: float = 1.0, serial_factory=None):
        self.device = device
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial_factory = serial_factory
        self.port = None
        self.msp: MSPClient | None = None
        self.cli: CLISession | None = None
        self.info = FCInfo(port=device)

    # ------------------------------------------------------------------
    def open(self) -> "FlightController":
        if self._serial_factory is not None:
            self.port = self._serial_factory(self.device)
        else:
            import serial

            self.port = serial.Serial(self.device, self.baudrate, timeout=0.05, write_timeout=2)
        time.sleep(0.1)
        try:
            self.port.reset_input_buffer()
        except Exception:
            pass
        self.msp = MSPClient(self.port, timeout=self.timeout, use_v2=False)
        return self

    def close(self) -> None:
        if self.port is not None:
            try:
                self.port.close()
            except Exception:
                pass
        self.port = None
        self.msp = None
        self.cli = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------
    def identify(self) -> FCInfo:
        assert self.msp is not None
        major, minor = self.msp.api_version()
        self.info.api_version = f"{major}.{minor}"
        # MSP v2 is available from API 1.40 (Betaflight 4.0)
        self.msp.use_v2 = (major, minor) >= (1, 40)
        self.info.variant = self.msp.fc_variant()
        if self.info.variant != "BTFL":
            raise MSPError(f"not a Betaflight FC (variant={self.info.variant!r})")
        self.info.version = self.msp.fc_version()
        try:
            self.info.board = self.msp.board_info()
        except MSPError:
            pass
        self.info.craft_name = self.msp.craft_name()
        try:
            st = self.msp.status()
            self.info.armed = st.armed
            self.info.pid_loop_hz = round(st.pid_loop_hz)
        except Exception:  # older firmware: status layout differs, not fatal
            pass
        return self.info

    def ensure_disarmed(self) -> None:
        """Refuse configuration writes while the craft is armed."""
        if self.msp is not None:
            try:
                if self.msp.status().armed:
                    raise MSPError("flight controller is ARMED - disarm and remove props before changing settings")
            except struct.error:
                pass

    # ------------------------------------------------------------------
    def cli_session(self) -> CLISession:
        if self.cli is None or not self.cli.active:
            self.cli = CLISession(self.port)
            self.cli.enter()
        return self.cli

    def diff_all(self) -> str:
        return self.cli_session().command("diff all", timeout=10)

    def dump_all(self) -> str:
        return self.cli_session().command("dump all", timeout=20)

    def get_all(self) -> str:
        """`get` with no argument lists every setting with its range / allowed values."""
        return self.cli_session().command("get", timeout=30)

    def save_and_reboot(self) -> None:
        if self.cli is not None and self.cli.active:
            self.cli.save()
        self.close()

    def exit_cli(self) -> None:
        if self.cli is not None and self.cli.active:
            self.cli.exit()
            self.close()

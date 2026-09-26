"""USB serial port discovery for Betaflight flight controllers."""

from __future__ import annotations

from dataclasses import dataclass

# (VID, PID) of USB-VCP implementations used by Betaflight targets.
KNOWN_FC_USB_IDS: dict[tuple[int, int], str] = {
    (0x0483, 0x5740): "STM32 Virtual COM Port",
    (0x2E3C, 0x5740): "AT32 Virtual COM Port",
    (0x0483, 0xDF11): "STM32 DFU (bootloader)",
}
# Generic USB-UART bridges sometimes found on older boards.
GENERIC_UART_IDS: dict[tuple[int, int], str] = {
    (0x10C4, 0xEA60): "CP210x",
    (0x0403, 0x6001): "FTDI",
    (0x1A86, 0x7523): "CH340",
}


@dataclass(frozen=True)
class FCPort:
    device: str
    vid: int | None
    pid: int | None
    description: str
    serial_number: str | None

    @property
    def is_dfu(self) -> bool:
        return (self.vid, self.pid) == (0x0483, 0xDF11)


def list_fc_ports(include_generic: bool = False) -> list[FCPort]:
    from serial.tools import list_ports

    known = dict(KNOWN_FC_USB_IDS)
    if include_generic:
        known.update(GENERIC_UART_IDS)
    ports = []
    for p in list_ports.comports():
        if p.vid is None:
            continue
        if (p.vid, p.pid) in known:
            ports.append(FCPort(p.device, p.vid, p.pid, known[(p.vid, p.pid)], p.serial_number))
    return [p for p in ports if not p.is_dfu]

"""Download blackbox dataflash contents, verify, then erase.

The erase step is only performed when the download is proven complete:

* every byte in ``[0, used_size)`` was received (addresses are contiguous),
* an optional second read pass matches the SHA-256 of the first pass,
* the saved file re-reads from disk with the same hash,
* the file contains at least one parseable blackbox log header.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .msp import MSPClient, MSPError

CHUNK_SIZE = 4096  # FC caps this to its MSP buffer size and tells us what it sent
LOG_START_MARKER = b"H Product:Blackbox flight data recorder by Nicholas Sherlock"


class DownloadError(Exception):
    pass


@dataclass
class DownloadResult:
    path: Path
    size: int
    sha256: str
    log_count: int
    verified: bool
    erased: bool = False
    extra_files: list[Path] = field(default_factory=list)


def sanitize(text: str) -> str:
    text = text.strip() or "unnamed"
    text = re.sub(r"[^\w.\-]+", "_", text, flags=re.UNICODE)
    return text.strip("_") or "unnamed"


def build_log_filename(craft_name: str, bf_version: str, when: datetime | None = None,
                       ext: str = ".bbl") -> str:
    """``<craft>_BF<version>_<YYYYmmdd-HHMMSS>.bbl``"""
    when = when or datetime.now()
    return f"{sanitize(craft_name)}_BF{sanitize(bf_version)}_{when:%Y%m%d-%H%M%S}{ext}"


def count_logs(data: bytes) -> int:
    return data.count(LOG_START_MARKER)


def read_dataflash(msp: MSPClient, used_size: int,
                   progress: Callable[[int, int], None] | None = None,
                   chunk_size: int = CHUNK_SIZE, max_retries: int = 5) -> bytes:
    out = bytearray()
    address = 0
    while address < used_size:
        want = min(chunk_size, used_size - address)
        for attempt in range(max_retries):
            try:
                chunk_addr, data = msp.dataflash_read(address, want)
                if chunk_addr != address:
                    raise MSPError(f"address mismatch: asked {address}, got {chunk_addr}")
                if not data:
                    raise MSPError("empty chunk")
                break
            except MSPError as e:
                if attempt == max_retries - 1:
                    raise DownloadError(f"dataflash read failed at 0x{address:08x}") from e
                time.sleep(0.05)
        data = data[: used_size - address]
        out.extend(data)
        address += len(data)
        if progress:
            progress(address, used_size)
    return bytes(out)


def download_blackbox(fc, out_dir: Path, *, verify_second_pass: bool = False,
                      erase: bool = True, save_config: bool = True,
                      progress: Callable[[int, int], None] | None = None,
                      log: Callable[[str], None] = print) -> DownloadResult | None:
    """Download the whole used area of the onboard dataflash.

    ``fc`` is an opened and identified :class:`FlightController`.
    Returns ``None`` when there is nothing to download.
    """
    msp = fc.msp
    summary = msp.dataflash_summary()
    if not summary.supported:
        sd = None
        try:
            sd = msp.sdcard_summary()
        except MSPError:
            pass
        if sd and sd.supported:
            raise DownloadError(
                "This FC logs to an SD card. Use `aidt msc` to reboot it into mass-storage mode "
                "and `aidt import <drive>` to copy the logs.")
        raise DownloadError("FC has no onboard dataflash")
    if summary.used_size == 0:
        log("Blackbox flash is empty - nothing to download.")
        return None

    info = fc.info
    now = datetime.now()
    craft_dir = Path(out_dir) / sanitize(info.craft_name)
    craft_dir.mkdir(parents=True, exist_ok=True)
    path = craft_dir / build_log_filename(info.craft_name, info.version, now)

    log(f"Downloading {summary.used_size} bytes of blackbox data from {info.craft_name or 'FC'} ...")
    t0 = time.monotonic()
    data = read_dataflash(msp, summary.used_size, progress)
    dt = time.monotonic() - t0
    log(f"Downloaded {len(data)} bytes in {dt:.1f}s ({len(data) / max(dt, 1e-6) / 1024:.1f} KiB/s)")

    if len(data) != summary.used_size:
        raise DownloadError(f"size mismatch: expected {summary.used_size}, got {len(data)}")
    digest = hashlib.sha256(data).hexdigest()

    if verify_second_pass:
        log("Verifying with a second read pass ...")
        data2 = read_dataflash(msp, summary.used_size)
        if hashlib.sha256(data2).hexdigest() != digest:
            raise DownloadError("verification pass differs from first download; flash NOT erased")

    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise DownloadError("file written to disk does not match downloaded data; flash NOT erased")

    log_count = count_logs(data)
    result = DownloadResult(path=path, size=len(data), sha256=digest, log_count=log_count, verified=True)

    meta = {
        "fc": info.to_dict(),
        "downloaded_at": now.isoformat(timespec="seconds"),
        "size": len(data),
        "sha256": digest,
        "log_count": log_count,
        "flash_total_size": summary.total_size,
    }
    if log_count == 0:
        log("warning: no blackbox log header found in data; flash NOT erased")
        erase = False

    if erase:
        log("Download verified. Erasing blackbox flash ...")
        msp.dataflash_erase()
        deadline = time.monotonic() + 300  # large chips can take minutes
        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                s = msp.dataflash_summary()
            except MSPError:
                continue
            if s.ready and s.used_size == 0:
                result.erased = True
                break
        if not result.erased:
            log("warning: erase did not complete within timeout")
        else:
            log("Blackbox flash erased.")
    if save_config:
        # Entering the CLI is done last: MSP no longer answers while in CLI mode.
        # The CLI session is left open for the caller (tuning) to reuse.
        try:
            diff = fc.diff_all()
            diff_path = path.with_name(path.stem + ".diff.txt")
            diff_path.write_text(diff, encoding="utf-8")
            result.extra_files.append(diff_path)
        except Exception as e:  # config backup is best effort
            log(f"warning: could not save `diff all`: {e}")

    meta["erased"] = result.erased
    meta_path = path.with_name(path.stem + ".json")
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    result.extra_files.append(meta_path)
    return result


def import_from_directory(src: Path, out_dir: Path, craft_name: str = "", bf_version: str = "") -> list[Path]:
    """Copy blackbox logs from an SD card mounted in mass-storage mode."""
    import shutil

    copied = []
    files = sorted(list(Path(src).rglob("*.BFL")) + list(Path(src).rglob("*.bfl")) +
                   list(Path(src).rglob("*.BBL")) + list(Path(src).rglob("*.bbl")))
    for f in files:
        head = f.read_bytes()[:4096]
        name, version = craft_name, bf_version
        m = re.search(rb"H Craft name:([^\n]*)\n", head)
        if m and not name:
            name = m.group(1).decode("utf-8", "replace")
        m = re.search(rb"H Firmware revision:Betaflight ([0-9.]+)", head)
        if m and not version:
            version = m.group(1).decode()
        when = datetime.fromtimestamp(f.stat().st_mtime)
        m = re.search(rb"H Log start datetime:(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", head)
        if m and not m.group(1).startswith(b"0000"):
            when = datetime.fromisoformat(m.group(1).decode())
        dest_dir = Path(out_dir) / sanitize(name)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / build_log_filename(name, version or "unknown", when)
        if dest.exists():
            dest = dest.with_stem(dest.stem + "_" + sanitize(f.stem))
        shutil.copy2(f, dest)
        copied.append(dest)
    return copied

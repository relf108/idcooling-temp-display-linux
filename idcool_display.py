#!/usr/bin/env python3
"""idcool-display - drive an ID-COOLING "Temp Display" cooler on Linux.

Shows the CPU temperature on ID-COOLING coolers whose little screen enumerates
as USB HID 1a86:e317 - e.g. the FX TD / FROZN TD "Temp Display" series, also
sold rebranded (the USB product string reads "IDCOOL-C", as shipped in some
Aftershock PCs).

The screen is a write-only USB-HID device: the host pushes a value and the
device firmware renders the number. There is no official Linux software, so
this daemon replicates the vendor protocol (decoded from the vendor Electron
app and verified on real hardware). Pure Python standard library - no deps.

Protocol - every command is a 64-byte HID output report:

    byte 0   : 0x55             header
    byte 1   : 0xBB             header
    byte 2   : 0x02             payload length
    byte 3   : command          1=CPU_TEMP, 2=CPU_FREQ, 3=CPU_USAGE, 4=SHOW
    byte 4   : value >> 8        16-bit big-endian
    byte 5   : value & 0xFF
    byte 6   : checksum = (0x55 + 0xBB + 0x02 + cmd + hi + lo) & 0xFF
    byte 7-63: 0x00 padding

The report is written to /dev/hidrawN prefixed with a 0x00 report-id byte
(the device uses unnumbered reports; the kernel strips the leading byte).

NOTE: this hardware can ONLY display the numbers its firmware knows how to
draw (temp / frequency / usage). It is not a framebuffer - arbitrary images
are not possible.

Usage:
    sudo ./idcool_display.py                 # show CPU temp, updated every 1s
    sudo ./idcool_display.py --metric usage  # show CPU usage % instead
    ./idcool_display.py --once --metric temp # one update and exit (for testing)

License: MIT.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import math
import os
import stat
import struct
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

USB_VENDOR_ID = 0x1A86
USB_PRODUCT_ID = 0xE317
USB_BUS_TYPE = 0x03
REPORT_LEN = 64
MIN_INTERVAL = 0.2  # Avoid accidentally flooding the USB device with reports.
MAX_CONSECUTIVE_FAILURES = 10
LOGGER = logging.getLogger(__name__)
Metric = Literal["temp", "usage", "freq"]

CMD_CPU_TEMPERATURE = 1
CMD_CPU_FREQUENCY = 2
CMD_CPU_USAGE = 3
CMD_SHOW = 4

# Values outside these ranges indicate a bad sensor read or programming error.
# Refusing them is safer than silently sending a surprising value to firmware.
COMMAND_RANGES: dict[int, tuple[int, int]] = {
    CMD_CPU_TEMPERATURE: (0, 150),
    CMD_CPU_FREQUENCY: (0, 20_000),
    CMD_CPU_USAGE: (0, 100),
    CMD_SHOW: (0, 1),
}

# Linux uapi: _IOR('H', 0x03, struct hidraw_devinfo). Defining the ioctl here
# keeps the driver dependency-free while allowing verification *after* open().
_IOC_READ = 2
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_HIDRAW_DEVINFO_FORMAT = "=IHH"  # bustype, vendor, product
_HIDRAW_DEVINFO_SIZE = struct.calcsize(_HIDRAW_DEVINFO_FORMAT)
HIDIOCGRAWINFO = (
    (_IOC_READ << _IOC_DIRSHIFT)
    | (ord("H") << _IOC_TYPESHIFT)
    | (0x03 << _IOC_NRSHIFT)
    | (_HIDRAW_DEVINFO_SIZE << _IOC_SIZESHIFT)
)


class DriverError(Exception):
    """An expected, user-facing driver failure."""


@dataclass
class Arguments(argparse.Namespace):
    """Typed values populated by ArgumentParser."""

    metric: Metric = "temp"
    interval: float = 1.0
    temp_path: Path | None = None
    device: Path | None = None
    wait: int = 60
    once: bool = False


# Argument validation
def interval_arg(text: str) -> float:
    """Parse a finite interval with a conservative USB rate limit."""
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("interval must be a number") from exc
    if not math.isfinite(value) or value < MIN_INTERVAL:
        raise argparse.ArgumentTypeError(
            f"interval must be finite and at least {MIN_INTERVAL} seconds"
        )
    return value


def nonnegative_int_arg(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
    return value


# Device discovery and verification
def _parse_hid_id(uevent: str) -> tuple[int, int, int] | None:
    """Return (bus, vendor, product) from a hidraw uevent, or None."""
    for line in uevent.splitlines():
        if not line.startswith("HID_ID="):
            continue
        try:
            values = line.split("=", 1)[1].split(":")
            if len(values) != 3:
                return None
            bus, vendor, product = (int(value, 16) for value in values)
            return bus, vendor, product
        except ValueError:
            return None
    return None


def find_device() -> Path | None:
    """Resolve the hidraw node by exact USB bus type and VID:PID from sysfs.

    Independent of any /dev symlink or udev/service ordering: the hidraw node
    exists as soon as the kernel enumerates the device. HID_ID looks like
    "0003:00001A86:0000E317".
    """
    for hidraw_path in sorted(Path("/sys/class/hidraw").glob("hidraw*")):
        try:
            identity = _parse_hid_id(_read_text(hidraw_path / "device/uevent"))
        except OSError:
            continue
        if identity == (USB_BUS_TYPE, USB_VENDOR_ID, USB_PRODUCT_ID):
            return Path("/dev") / hidraw_path.name
    return None


def wait_for_device(timeout: int) -> Path:
    """Wait up to timeout seconds, checking once even when timeout is zero."""
    deadline = time.monotonic() + timeout
    while True:
        dev = find_device()
        if dev:
            return dev
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))
    raise DriverError(
        f"ID-COOLING display ({USB_VENDOR_ID:04X}:{USB_PRODUCT_ID:04X}) "
        + "not found; check that it is connected"
    )


def verify_open_device(fd: int, path: Path) -> None:
    """Verify the opened fd, closing the identify-then-open race.

    This check also applies to --device, preventing an accidental write to an
    unrelated hidraw peripheral.
    """
    mode = os.fstat(fd).st_mode
    if not stat.S_ISCHR(mode):
        raise DriverError(f"{path} is not a character device")

    info = bytearray(_HIDRAW_DEVINFO_SIZE)
    try:
        _ = fcntl.ioctl(fd, HIDIOCGRAWINFO, info, True)
    except OSError as exc:
        raise DriverError(f"{path} is not a usable hidraw device: {exc}") from exc

    bus, vendor, product = struct.unpack(_HIDRAW_DEVINFO_FORMAT, info)
    if (bus, vendor, product) != (USB_BUS_TYPE, USB_VENDOR_ID, USB_PRODUCT_ID):
        raise DriverError(
            f"refusing to use {path}: expected USB "
            + f"{USB_VENDOR_ID:04x}:{USB_PRODUCT_ID:04x}, got bus {bus} "
            + f"{vendor:04x}:{product:04x}"
        )


def open_device(path: Path) -> int:
    """Open and then verify a display device."""
    flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DriverError(f"cannot open {path}: {exc}") from exc
    verified = False
    try:
        verify_open_device(fd, path)
        verified = True
    finally:
        if not verified:
            os.close(fd)
    return fd


# Protocol encoding
def frame(cmd: int, value: int | float) -> bytes:
    """Encode one validated 64-byte command report."""
    if cmd not in COMMAND_RANGES:
        raise ValueError(f"unknown display command: {cmd!r}")

    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"command value is not numeric: {value!r}") from exc
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise ValueError(f"command value must be a finite integer: {value!r}")

    value = int(numeric)
    minimum, maximum = COMMAND_RANGES[cmd]
    if not minimum <= value <= maximum:
        raise ValueError(
            f"value {value} is outside the safe range {minimum}..{maximum} "
            + f"for command {cmd}"
        )

    hi, lo = (value >> 8) & 0xFF, value & 0xFF
    checksum = (0x55 + 0xBB + 0x02 + cmd + hi + lo) & 0xFF
    report = bytearray(REPORT_LEN)
    report[0:7] = bytes([0x55, 0xBB, 0x02, cmd, hi, lo, checksum])
    return bytes(report)


def write_report(fd: int, report: bytes) -> None:
    """Write exactly one HID report and reject an unexpected short write."""
    if len(report) != REPORT_LEN:
        raise ValueError(f"report must be exactly {REPORT_LEN} bytes")

    # hidraw requires a report-number byte first. This device has no numbered
    # reports, so report number zero precedes the 64-byte vendor report.
    payload = b"\x00" + report
    written = os.write(fd, payload)
    if written != len(payload):
        # Do not write the suffix as another operation: hidraw may interpret it
        # as a second, malformed report. Failing allows systemd to reconnect.
        raise DriverError(f"short HID write: {written}/{len(payload)} bytes")


# Sensor readings
def _read_text(path: Path) -> str:
    with path.open(encoding="ascii") as fh:
        return fh.read().strip()


def find_temp_path(hwmon_root: Path | None = None) -> Path:
    """Find a known CPU package sensor, never an arbitrary hwmon input."""
    if hwmon_root is None:
        hwmon_root = Path("/sys/class/hwmon")
    hwmons: dict[Path, str] = {}
    for hwmon_path in sorted(hwmon_root.glob("hwmon*")):
        try:
            hwmons[hwmon_path] = _read_text(hwmon_path / "name")
        except OSError:
            continue

    def labelled(hwmon_path: Path, wanted: str) -> Path | None:
        for label in sorted(hwmon_path.glob("temp*_label")):
            try:
                if _read_text(label) == wanted:
                    path = label.with_name(label.stem[: -len("_label")] + "_input")
                    if not path.is_file():
                        continue
                    return path
            except OSError:
                continue
        return None

    for driver_name, label in (
        ("k10temp", "Tctl"),
        ("zenpower", "Tctl"),
        ("coretemp", "Package id 0"),
    ):
        for hwmon_path, name in hwmons.items():
            if name != driver_name:
                continue
            fallback = hwmon_path / "temp1_input"
            candidate = labelled(hwmon_path, label)
            if candidate:
                return candidate
            if fallback.is_file():
                return fallback

    detected = ", ".join(sorted(set(hwmons.values()))) or "none"
    raise DriverError(
        "no supported CPU temperature sensor found "
        + f"(detected hwmon drivers: {detected}); select one with --temp-path"
    )


def read_temp_c(path: Path) -> float:
    try:
        millidegrees = int(_read_text(path))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read temperature from {path}: {exc}") from exc

    temperature = millidegrees / 1000.0
    minimum, maximum = COMMAND_RANGES[CMD_CPU_TEMPERATURE]
    if not minimum <= temperature <= maximum:
        raise ValueError(
            f"implausible CPU temperature from {path}: {temperature:.1f} °C "
            + f"(expected {minimum}..{maximum} °C)"
        )
    return temperature


def read_cpu_usage() -> float:
    """Whole-system CPU usage percentage over a short sample."""

    def snap() -> tuple[int, int]:
        try:
            fields = _read_text(Path("/proc/stat")).splitlines()[0].split()
            if not fields or fields[0] != "cpu" or len(fields) < 5:
                raise ValueError("unexpected first line")
            # guest and guest_nice are already included in user and nice.
            counters = [int(value) for value in fields[1:9]]
        except (OSError, ValueError, IndexError) as exc:
            raise ValueError(f"cannot parse /proc/stat: {exc}") from exc
        idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
        return sum(counters), idle

    total0, idle0 = snap()
    time.sleep(0.2)
    total1, idle1 = snap()
    elapsed = total1 - total0
    if elapsed <= 0:
        raise ValueError("CPU counters did not advance")
    return max(0.0, min(100.0, 100.0 * (1 - (idle1 - idle0) / elapsed)))


def read_cpu_freq_mhz() -> float:
    """Return average current CPU frequency, failing rather than displaying 0."""
    frequencies: list[float] = []
    for path in Path("/sys/devices/system/cpu").glob("cpu*/cpufreq/scaling_cur_freq"):
        try:
            frequency = int(_read_text(path)) / 1000.0  # kHz -> MHz
            if 0 < frequency <= COMMAND_RANGES[CMD_CPU_FREQUENCY][1]:
                frequencies.append(frequency)
        except (OSError, ValueError):
            continue
    if not frequencies:
        raise ValueError("no valid CPU frequency values found in sysfs")
    return sum(frequencies) / len(frequencies)


# Sampling and command-line interface
def make_sample(metric: Metric, temp_path: Path | None) -> tuple[int, int]:
    """Return a validated command/value pair for the selected metric."""
    if metric == "temp":
        if temp_path is None:
            raise ValueError("temperature metric requires a sensor path")
        return CMD_CPU_TEMPERATURE, round(read_temp_c(temp_path))
    if metric == "usage":
        return CMD_CPU_USAGE, round(read_cpu_usage())
    if metric == "freq":
        return CMD_CPU_FREQUENCY, round(read_cpu_freq_mhz())
    raise ValueError(metric)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ID-COOLING Temp Display driver")
    _ = parser.add_argument(
        "--metric", choices=["temp", "usage", "freq"], default="temp"
    )
    _ = parser.add_argument(
        "--interval",
        type=interval_arg,
        default=1.0,
        help=f"seconds between updates (minimum {MIN_INTERVAL})",
    )
    _ = parser.add_argument(
        "--temp-path",
        type=Path,
        help="override hwmon temperature input path",
    )
    _ = parser.add_argument(
        "--device",
        type=Path,
        help="override hidraw path; VID/PID verification enforced",
    )
    _ = parser.add_argument(
        "--wait",
        type=nonnegative_int_arg,
        default=60,
        help="non-negative seconds to wait for the device",
    )
    _ = parser.add_argument(
        "--once", action="store_true", help="send one update and exit"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = Arguments()
    _ = build_argument_parser().parse_args(argv, namespace=args)
    temp_path = args.temp_path or (find_temp_path() if args.metric == "temp" else None)
    device_path = args.device or wait_for_device(args.wait)
    fd = open_device(device_path)

    try:
        write_report(fd, frame(CMD_SHOW, 1))
        failures = 0
        while True:
            try:
                cmd, value = make_sample(args.metric, temp_path)
                write_report(fd, frame(cmd, value))
                failures = 0
            except ValueError as exc:
                # Skip transient failures, but let the service manager restart
                # after persistent failures. One-shot mode fails immediately.
                if args.once:
                    raise DriverError(str(exc)) from exc
                failures += 1
                LOGGER.warning(
                    "skipping invalid sensor sample (%d/%d): %s",
                    failures, MAX_CONSECUTIVE_FAILURES, exc,
                )
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    raise DriverError(
                        f"sensor sampling failed {failures} times in a row: {exc}"
                    ) from exc

            if args.once:
                break
            time.sleep(args.interval)
    finally:
        os.close(fd)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (DriverError, OSError) as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(1) from None

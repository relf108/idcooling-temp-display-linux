from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest import mock

import idcool_display as driver


class FrameTests(unittest.TestCase):
    def test_known_temperature_frame(self) -> None:
        report = driver.frame(driver.CMD_CPU_TEMPERATURE, 77)
        self.assertEqual(len(report), 64)
        self.assertEqual(report[:7], bytes.fromhex("55 bb 02 01 00 4d 60"))
        self.assertEqual(report[7:], bytes(57))

    def test_rejects_unknown_command(self) -> None:
        with self.assertRaises(ValueError):
            driver.frame(99, 1)

    def test_rejects_out_of_range_values(self) -> None:
        with self.assertRaises(ValueError):
            driver.frame(driver.CMD_CPU_USAGE, 101)
        with self.assertRaises(ValueError):
            driver.frame(driver.CMD_CPU_TEMPERATURE, 65_535)
        with self.assertRaises(ValueError):
            driver.frame(driver.CMD_SHOW, -1)

    def test_rejects_non_finite_and_fractional_values(self) -> None:
        for value in (float("nan"), float("inf"), 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                driver.frame(driver.CMD_CPU_USAGE, value)


class WriteTests(unittest.TestCase):
    def test_writes_report_id_and_complete_report(self) -> None:
        report = driver.frame(driver.CMD_SHOW, 1)
        with mock.patch.object(driver.os, "write", return_value=65) as write:
            driver.write_report(7, report)
        write.assert_called_once_with(7, b"\x00" + report)

    def test_rejects_short_write(self) -> None:
        report = driver.frame(driver.CMD_SHOW, 1)
        with mock.patch.object(driver.os, "write", return_value=64):
            with self.assertRaises(driver.DriverError):
                driver.write_report(7, report)

    def test_rejects_wrong_report_length(self) -> None:
        with self.assertRaises(ValueError):
            driver.write_report(7, bytes(63))


class HidIdTests(unittest.TestCase):
    def test_parses_identity_among_other_uevent_fields(self) -> None:
        for identity in ("0003:00001A86:0000E317", "0003:00001a86:0000e317"):
            with self.subTest(identity=identity):
                self.assertEqual(
                    driver._parse_hid_id(
                        f"DRIVER=hid-generic\nHID_ID={identity}\nHID_NAME=IDCOOL-C\n"
                    ),
                    (0x03, 0x1A86, 0xE317),
                )

    def test_missing_hid_id(self) -> None:
        for text in (
            "", "HID_NAME=IDCOOL-C\n", "OTHER_HID_ID=0003:00001A86:0000E317"
        ):
            with self.subTest(text=text):
                self.assertIsNone(driver._parse_hid_id(text))

    def test_wrong_field_count(self) -> None:
        for identity in ("", "0003:00001A86", "0003:00001A86:0000E317:0000"):
            with self.subTest(identity=identity):
                self.assertIsNone(driver._parse_hid_id(f"HID_ID={identity}\n"))

    def test_non_hex_field(self) -> None:
        for identity in ("oops:1A86:E317", "0003:oops:E317", "0003:1A86:oops"):
            with self.subTest(identity=identity):
                self.assertIsNone(driver._parse_hid_id(f"HID_ID={identity}\n"))


class DeviceTests(unittest.TestCase):
    @staticmethod
    def _ioctl_identity(
        bus: int, vendor: int, product: int
    ) -> Callable[[int, int, bytearray, bool], int]:
        def fake_ioctl(
            _fd: int, request: int, buffer: bytearray, mutate: bool
        ) -> int:
            if request != driver.HIDIOCGRAWINFO or not mutate:
                raise AssertionError("unexpected ioctl call")
            buffer[:] = struct.pack("=IHH", bus, vendor, product)
            return 0

        return fake_ioctl

    @mock.patch.object(driver.os, "fstat")
    def test_accepts_expected_open_device(self, fstat: mock.MagicMock) -> None:
        fstat.return_value.st_mode = stat.S_IFCHR
        ioctl = self._ioctl_identity(
            driver.USB_BUS_TYPE, driver.USB_VENDOR_ID, driver.USB_PRODUCT_ID
        )
        with mock.patch.object(driver.fcntl, "ioctl", side_effect=ioctl):
            driver.verify_open_device(7, Path("/dev/hidraw7"))

    @mock.patch.object(driver.os, "fstat")
    def test_rejects_wrong_open_device(self, fstat: mock.MagicMock) -> None:
        fstat.return_value.st_mode = stat.S_IFCHR
        ioctl = self._ioctl_identity(driver.USB_BUS_TYPE, 0x1234, 0x5678)
        with mock.patch.object(driver.fcntl, "ioctl", side_effect=ioctl):
            with self.assertRaises(driver.DriverError):
                driver.verify_open_device(7, Path("/dev/hidraw7"))

    @mock.patch.object(driver.os, "fstat")
    def test_rejects_non_character_device(self, fstat: mock.MagicMock) -> None:
        fstat.return_value.st_mode = stat.S_IFREG
        with self.assertRaises(driver.DriverError):
            driver.verify_open_device(7, Path("/tmp/not-a-device"))


class SensorTests(unittest.TestCase):
    @staticmethod
    def _make_hwmon(root: Path, name: str, files: dict[str, str]) -> Path:
        hwmon = root / "hwmon0"
        hwmon.mkdir()
        _ = (hwmon / "name").write_text(name, encoding="ascii")
        for filename, value in files.items():
            _ = (hwmon / filename).write_text(value, encoding="ascii")
        return hwmon

    def test_selects_amd_tctl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hwmon = self._make_hwmon(
                root,
                "k10temp",
                {
                    "temp1_label": "Tdie\n",
                    "temp1_input": "60000\n",
                    "temp2_label": "Tctl\n",
                    "temp2_input": "65000\n",
                },
            )
            self.assertEqual(driver.find_temp_path(root), hwmon / "temp2_input")

    def test_selects_zenpower_tctl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hwmon = self._make_hwmon(
                root,
                "zenpower",
                {
                    "temp1_label": "Tdie\n",
                    "temp1_input": "60000\n",
                    "temp2_label": "Tctl\n",
                    "temp2_input": "65000\n",
                },
            )
            self.assertEqual(driver.find_temp_path(root), hwmon / "temp2_input")

    def test_skips_matching_label_with_missing_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hwmon = self._make_hwmon(
                root,
                "k10temp",
                {
                    "temp1_input": "60000\n",
                    "temp2_label": "Tctl\n",
                    "temp3_label": "Tctl\n",
                    "temp3_input": "65000\n",
                },
            )
            self.assertEqual(driver.find_temp_path(root), hwmon / "temp3_input")

    def test_selects_intel_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hwmon = self._make_hwmon(
                root,
                "coretemp",
                {
                    "temp1_label": "Package id 0\n",
                    "temp1_input": "55000\n",
                },
            )
            self.assertEqual(driver.find_temp_path(root), hwmon / "temp1_input")

    def test_refuses_unrelated_sensor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._make_hwmon(root, "nvme", {"temp1_input": "45000\n"})
            with self.assertRaises(driver.DriverError):
                driver.find_temp_path(root)

    def test_reads_millidegrees(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as fh:
            _ = fh.write("77500\n")
            path = Path(fh.name)
        try:
            self.assertEqual(driver.read_temp_c(path), 77.5)
        finally:
            path.unlink()

    def test_rejects_implausible_temperature(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as fh:
            _ = fh.write("999000\n")
            path = Path(fh.name)
        try:
            with self.assertRaises(ValueError):
                driver.read_temp_c(path)
        finally:
            path.unlink()

    def test_cpu_usage_does_not_double_count_guest_time(self) -> None:
        snapshots = (
            "cpu 100 0 0 100 0 0 0 0 50 0\n",
            "cpu 110 0 0 110 0 0 0 0 1050 0\n",
        )
        with (
            mock.patch.object(driver, "_read_text", side_effect=snapshots),
            mock.patch.object(driver.time, "sleep"),
        ):
            self.assertEqual(driver.read_cpu_usage(), 50.0)


class SampleTests(unittest.TestCase):
    def test_rejects_unknown_metric_without_reading_frequency(self) -> None:
        with mock.patch.object(
            driver, "read_cpu_freq_mhz", return_value=3000
        ) as read:
            with self.assertRaisesRegex(ValueError, "unknown"):
                driver.make_sample("unknown", None)  # type: ignore[arg-type]
        read.assert_not_called()

    def test_frequency_metric(self) -> None:
        with mock.patch.object(driver, "read_cpu_freq_mhz", return_value=3000.4):
            self.assertEqual(
                driver.make_sample("freq", None), (driver.CMD_CPU_FREQUENCY, 3000)
            )


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.open_device = stack.enter_context(
            mock.patch.object(driver, "open_device", return_value=7)
        )
        self.close = stack.enter_context(mock.patch.object(driver.os, "close"))
        self.write = stack.enter_context(mock.patch.object(driver, "write_report"))
        self.sample = stack.enter_context(mock.patch.object(driver, "make_sample"))
        self.sleep = stack.enter_context(mock.patch.object(driver.time, "sleep"))
        self.warning = stack.enter_context(mock.patch.object(driver.LOGGER, "warning"))
        self.argv = ["--device", "/dev/hidraw7", "--temp-path", "/missing/temp_input"]

    def test_persistent_failure_stops_after_ten_samples(self) -> None:
        error = ValueError("sensor unavailable")
        # A finite sequence makes a missing cap fail rather than hang the suite.
        self.sample.side_effect = [error] * 10 + [AssertionError("retry cap exceeded")]
        with self.assertRaisesRegex(
            driver.DriverError, "failed 10 times in a row"
        ) as raised:
            driver.main(self.argv)
        self.assertIs(raised.exception.__cause__, error)
        self.assertEqual(self.sample.call_count, 10)
        self.assertEqual(self.warning.call_count, 10)
        self.warning.assert_called_with(
            "skipping invalid sensor sample (%d/%d): %s", 10, 10, error
        )
        self.assertEqual(self.sleep.call_count, 9)
        self.write.assert_called_once_with(7, driver.frame(driver.CMD_SHOW, 1))
        self.close.assert_called_once_with(7)

    def test_success_resets_consecutive_failure_count(self) -> None:
        error = ValueError("sensor unavailable")
        self.sample.side_effect = (
            [error] * 9
            + [(driver.CMD_CPU_TEMPERATURE, 77)]
            + [error] * 10
            + [AssertionError("retry cap exceeded")]
        )
        with self.assertRaisesRegex(driver.DriverError, "failed 10 times in a row"):
            driver.main(self.argv)
        self.assertEqual(self.sample.call_count, 20)
        self.assertEqual(self.sleep.call_count, 19)
        self.assertEqual(self.warning.call_count, 19)
        self.assertEqual(self.warning.call_args_list[9].args[1:3], (1, 10))
        self.assertEqual(self.write.call_args_list, [
            mock.call(7, driver.frame(driver.CMD_SHOW, 1)),
            mock.call(7, driver.frame(driver.CMD_CPU_TEMPERATURE, 77)),
        ])
        self.close.assert_called_once_with(7)

    def test_once_fails_immediately_and_closes_device(self) -> None:
        error = ValueError("sensor unavailable")
        self.sample.side_effect = error
        with self.assertRaisesRegex(
            driver.DriverError, "sensor unavailable"
        ) as raised:
            driver.main(self.argv + ["--once"])
        self.assertIs(raised.exception.__cause__, error)
        self.sample.assert_called_once()
        self.sleep.assert_not_called()
        self.warning.assert_not_called()
        self.write.assert_called_once_with(7, driver.frame(driver.CMD_SHOW, 1))
        self.close.assert_called_once_with(7)

    def test_once_writes_one_sample_and_closes_device(self) -> None:
        self.sample.return_value = (driver.CMD_CPU_TEMPERATURE, 77)
        driver.main(self.argv + ["--once"])
        self.sample.assert_called_once()
        self.assertEqual(self.write.call_args_list, [
            mock.call(7, driver.frame(driver.CMD_SHOW, 1)),
            mock.call(7, driver.frame(driver.CMD_CPU_TEMPERATURE, 77)),
        ])
        self.sleep.assert_not_called()
        self.warning.assert_not_called()
        self.close.assert_called_once_with(7)


class ArgumentTests(unittest.TestCase):
    def test_parser_populates_typed_namespace(self) -> None:
        args = driver.Arguments()
        _ = driver.build_argument_parser().parse_args(
            ["--metric", "usage", "--device", "/dev/hidraw7"], namespace=args
        )
        self.assertEqual(args.metric, "usage")
        self.assertEqual(args.device, Path("/dev/hidraw7"))
        self.assertIsNone(args.temp_path)

    def test_interval_rate_limit(self) -> None:
        self.assertEqual(driver.interval_arg("1"), 1.0)
        for value in ("0", "-1", "nan", "inf", "0.1"):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                driver.interval_arg(value)

    def test_wait_must_be_nonnegative(self) -> None:
        self.assertEqual(driver.nonnegative_int_arg("0"), 0)
        with self.assertRaises(argparse.ArgumentTypeError):
            driver.nonnegative_int_arg("-1")


if __name__ == "__main__":
    unittest.main()

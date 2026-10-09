"""Тесты контрольных сумм MS43 (``MS43/tools/checksum.py``)."""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from MS43.tools import checksum as ck

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "MS43" / "firmware"
ARCHIVE = ROOT / "MS43" / "firmware_archive"

REFERENCE = FIRMWARE / "Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin"
KNOWN_BAD_CAL = FIRMWARE / "Siemens_MS43_MS430069_E39_M54B25_EU4_LHD.bin"

#: Ожидания по эталонному стоку MS430069 E46 M54B30 EU4 LHD.
REFERENCE_RECORDS = {
    "boot": (0xC6D5, 0x2D2D, [(0x00000000, 0x0000308D)]),
    "prog": (
        0x727E,
        0x3030,
        [
            (0x90000, 0x9FFFF),
            (0xA0000, 0xAFFFB),
            (0xB0000, 0xBFCB9),
            (0xC0000, 0xCA8CB),
            (0xD0000, 0xDFFF7),
            (0xE0000, 0xEEFFF),
        ],
    ),
    "cal": (0xE966, 0x3936, [(0x70000, 0x72FFF), (0x74000, 0x7EE17)]),
}


class CrcAlgorithmTests(unittest.TestCase):
    def test_crc16_arc_check_value(self) -> None:
        # Общепринятое проверочное значение CRC-16/ARC для "123456789".
        self.assertEqual(ck.crc16_arc(b"123456789"), 0xBB3D)
        self.assertEqual(ck.crc16_arc_table(b"123456789"), 0xBB3D)

    def test_crc16_table_matches_bitwise(self) -> None:
        payload = bytes(range(256)) * 3
        for init in (0x0000, 0x2D2D, 0x3030, 0x3936, 0xFFFF):
            self.assertEqual(
                ck.crc16_arc(payload, init), ck.crc16_arc_table(payload, init), hex(init)
            )

    def test_crc16_zeros_is_a_real_prefix(self) -> None:
        self.assertEqual(ck.crc16_zeros(0), ck.crc16_arc(b""))
        self.assertEqual(ck.crc16_zeros(9000), ck.crc16_arc_table(bytes(9000)))

    def test_crc16_chaining_equals_concatenation(self) -> None:
        first, second = b"abc", bytes(range(64))
        chained = ck.crc16_arc_table(second, ck.crc16_arc_table(first, 0x1234))
        self.assertEqual(chained, ck.crc16_arc_table(first + second, 0x1234))


class LayoutTests(unittest.TestCase):
    def test_reference_records_exact(self) -> None:
        image = REFERENCE.read_bytes()
        for name, (stored, init, segments) in REFERENCE_RECORDS.items():
            spec = {
                "boot": ck.RECORD_BOOT,
                "prog": ck.RECORD_PROG,
                "cal": ck.RECORD_CAL,
            }[name]
            got_stored, got_segments = ck.parse_record(image, spec)
            self.assertEqual(got_stored, stored, name)
            self.assertEqual(got_segments, segments, name)
            self.assertEqual(ck.read_init(image, spec), init, name)
            self.assertEqual(ck.compute_record(image, spec, init), stored, name)

    def test_program_init_comes_from_zif_field(self) -> None:
        image = REFERENCE.read_bytes()
        self.assertEqual(image[0x6FFB6:0x6FFB8], b"00")
        self.assertEqual(ck.read_init_program(image), 0x3030)

    def test_cal_init_comes_from_version_digits(self) -> None:
        image = REFERENCE.read_bytes()
        self.assertEqual(image[0x70008:0x7000E], b"430069")
        self.assertEqual(ck.read_init_cal(image, 0x70000), 0x3936)

    def test_boot_init_is_dash_dash(self) -> None:
        image = REFERENCE.read_bytes()
        self.assertEqual(image[0x3FE6:0x3FE8], b"--")
        self.assertEqual(ck.read_init_boot(image), 0x2D2D)

    def test_unknown_size_is_refused(self) -> None:
        with self.assertRaises(ck.MS43Error):
            ck.identify_layout(0x4000)

    def test_corrupt_segment_table_is_refused_not_guessed(self) -> None:
        image = bytearray(REFERENCE.read_bytes())
        image[ck.RECORD_PROG.offset + 2 : ck.RECORD_PROG.offset + 4] = (0).to_bytes(2, "little")
        with self.assertRaises(ck.MS43Error):
            ck.parse_record(bytes(image), ck.RECORD_PROG)

    def test_segment_out_of_image_is_refused(self) -> None:
        image = bytearray(REFERENCE.read_bytes())
        image[ck.RECORD_CAL.offset + 4 + 4 : ck.RECORD_CAL.offset + 4 + 8] = (0x7FFFFF).to_bytes(
            4, "little"
        )
        with self.assertRaises(ck.MS43Error):
            ck.compute_record(bytes(image), ck.RECORD_CAL, 0x3936)


class CorpusTests(unittest.TestCase):
    """Схема обязана сходиться на всём корпусе, кроме документированных исключений."""

    def _scan(self, directory: Path) -> tuple[int, list[tuple[str, str]]]:
        total = 0
        failures: list[tuple[str, str]] = []
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() != ".bin":
                continue
            try:
                report = ck.verify_image(path)
            except ck.MS43Error as error:  # pragma: no cover - диагностика
                failures.append((path.name, f"ошибка раскладки: {error}"))
                continue
            total += 1
            for record in report.failures:
                failures.append((path.name, record.name))
        return total, failures

    def test_stock_firmware_verified(self) -> None:
        total, failures = self._scan(FIRMWARE)
        self.assertEqual(total, 18)
        expected = {("Siemens_MS43_MS430069_E39_M54B25_EU4_LHD.bin", "cal")}
        self.assertEqual(set(failures), expected)

    def test_firmware_archive_verified(self) -> None:
        total, failures = self._scan(ARCHIVE)
        self.assertEqual(total, 33)
        expected = {
            ("E39_525__b137b56g_Ca430037_2.5L_Stok.bin", "prog"),
            ("E39_525__b137b57g_Ca430037_Stok.bin", "prog"),
            ("E53__E39_b137b56g_Ca430037_2.5L_Full_Stok.bin", "prog"),
        }
        self.assertEqual(set(failures), expected)
        # каждое расхождение обязано быть описано в KNOWN_ANOMALIES с причиной
        for name, record in failures:
            self.assertIn(name, ck.KNOWN_ANOMALIES)
            self.assertIn(record, ck.KNOWN_ANOMALIES[name])

    def test_known_anomaly_cal_is_three_bytes_from_e46_sibling(self) -> None:
        """Сток «E39 M54B25» отличается от E46-собрата тремя байтами."""
        e39 = KNOWN_BAD_CAL.read_bytes()
        e46 = (FIRMWARE / "Siemens_MS43_MS430069_E46_M54B25_EU4_LHD.bin").read_bytes()
        diff = [offset for offset in range(0x70000, 0x80000) if e39[offset] != e46[offset]]
        self.assertEqual([hex(offset) for offset in diff], ["0x70ab1", "0x70b1a", "0x70b1e"])

    def test_monitor_sums_are_reported_but_never_checked(self) -> None:
        report = ck.verify_image(REFERENCE)
        self.assertEqual(report.monitor["prog_mon @ 0x6FDAE"], 0xB3DAAD17)
        self.assertEqual(report.monitor["cal_mon @ 0x72FFC"], 0xA61EB8D5)
        self.assertTrue(any("unverified" in note for note in report.notes))

    def test_all_cal64_reads_share_the_version_init_rule(self) -> None:
        seen: dict[str, int] = {}
        for path in sorted(ARCHIVE.iterdir()):
            data = path.read_bytes()
            if len(data) != ck.IMAGE_SIZE_CAL64:
                continue
            init = ck.read_init_cal(data, 0x0)
            # init = LE16 из двух последних цифр кода версии (0x0C, 0x0D)
            self.assertEqual(init, (data[0x0D] << 8) | data[0x0C])
            self.assertEqual(data[0x08:0x0E], self._version_of(data).encode())
            seen[self._version_of(data)] = init
        self.assertEqual(
            seen,
            {
                "430037": 0x3733,
                "430055": 0x3535,
                "430056": 0x3635,
                "430064": 0x3436,
                "430066": 0x3636,
                "430069": 0x3936,
            },
        )

    @staticmethod
    def _version_of(data: bytes) -> str:
        return data[0x08:0x0E].decode("ascii")


class RepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ms43-checksum-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_repair_roundtrip_changes_only_crc_slots(self) -> None:
        original = REFERENCE.read_bytes()
        broken = bytearray(original)
        broken[ck.RECORD_PROG.offset] ^= 0xFF
        broken[ck.RECORD_CAL.offset + 1] ^= 0x01
        source = self.tmp / "broken.bin"
        source.write_bytes(bytes(broken))
        target = self.tmp / "fixed.bin"

        result = ck.repair_image(source, target)
        self.assertTrue(result.ok)

        fixed = target.read_bytes()
        # отремонтированный файл побайтно равен исходному стоку
        self.assertEqual(fixed, original)
        # а относительно испорченного изменились только позиции CRC
        changed = {index for index, (a, b) in enumerate(zip(bytes(broken), fixed)) if a != b}
        self.assertTrue(changed)
        self.assertTrue(
            changed
            <= {ck.RECORD_PROG.offset, ck.RECORD_PROG.offset + 1,
                ck.RECORD_CAL.offset, ck.RECORD_CAL.offset + 1}
        )
        self.assertEqual(ck.verify_image(target).failures, [])
        # исходный файл не тронут
        self.assertEqual(source.read_bytes(), bytes(broken))

    def test_repair_refuses_broken_table(self) -> None:
        broken = bytearray(REFERENCE.read_bytes())
        broken[ck.RECORD_CAL.offset + 2 : ck.RECORD_CAL.offset + 4] = (0xFF).to_bytes(2, "little")
        source = self.tmp / "broken_table.bin"
        source.write_bytes(bytes(broken))
        with self.assertRaises(ck.MS43Error):
            ck.repair_image(source, self.tmp / "never.bin")
        self.assertFalse((self.tmp / "never.bin").exists())

    def test_solve_init_recovers_init_of_a_healthy_image(self) -> None:
        image = REFERENCE.read_bytes()
        for spec, expected in (
            (ck.RECORD_BOOT, 0x2D2D),
            (ck.RECORD_PROG, 0x3030),
            (ck.RECORD_CAL, 0x3936),
        ):
            self.assertEqual(ck.solve_init(image, spec), expected, spec.name)

    def test_solve_init_shows_init_needed_by_a_modified_image(self) -> None:
        image = bytearray(KNOWN_BAD_CAL.read_bytes())
        spec = ck.RECORD_CAL
        derived = ck.solve_init(bytes(image), spec)
        self.assertIsNotNone(derived)
        self.assertNotEqual(derived, ck.read_init_cal(bytes(image), 0x70000))
        self.assertEqual(ck.compute_record(bytes(image), spec, derived), 0x5ADF)


class CliTests(unittest.TestCase):
    def test_exit_codes(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ck.main([str(REFERENCE)]), 0)
            self.assertEqual(ck.main([str(KNOWN_BAD_CAL)]), 1)
            self.assertEqual(ck.main(["/nonexistent/image.bin"]), 2)


if __name__ == "__main__":
    unittest.main()

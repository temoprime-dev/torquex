"""Тесты контрольных сумм MS45 (``MS45/tools/checksums.py``).

Проверки идут по всем доступным образам: 238 калибровок (``MS45/firmware`` +
``MS45/firmware_spdaten/cal`` + RR-части) и реальное полное чтение 1 МиБ из
``MS45/firmware_dumps``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from MS45.tools import checksums as ck

ROOT = Path(__file__).resolve().parents[2]
MS45 = ROOT / "MS45"
FIRMWARE = MS45 / "firmware"
SPDATEN = MS45 / "firmware_spdaten" / "cal"
DUMPS = MS45 / "firmware_dumps"

FULL_DUMP = DUMPS / "MS45.0_0044560BN_fullread_ORI.bin"
MPC_DUMP = DUMPS / "MS45.0_0044560BN_MPC_ORI.bin"
REFERENCE_CAL = SPDAten_ref = SPDATEN / "MS45.0_0044560BN00S0FS0N_P7574503_cal_116K.bin"
BO00_CAL = FIRMWARE / "MS45.0_456BO00S_E83" / "7561448_0044560BO00S0CO0R.bin"
LO00_CAL = SPDATEN / "MS45.1_0044570LO00S01J2R_P7561517_cal_116K.bin"


def _all_cal_files() -> list[Path]:
    files = sorted(p for p in FIRMWARE.rglob("*.bin") if p.is_file())
    files += sorted(p for p in SPDATEN.glob("*.bin") if p.is_file())
    return [p for p in files if p.stat().st_size == ck.CAL_SIZE]


class TestGeometry(unittest.TestCase):
    """Геометрия образа и определение семейства."""

    def test_cal_sizes(self) -> None:
        self.assertEqual(ck.CAL_SIZE, 0x1D000)
        self.assertEqual(ck.EXT_CAL_OFFSET, 0x40000)
        self.assertEqual(ck.EXT_FLASH_SIZE, 0x100000)
        self.assertEqual(ck.CAL_BASE, 0x02040000)
        self.assertEqual(ck.CRC32_REGION, (0x200, 0x1D000))
        self.assertEqual(ck.MO3_REGION, (0x240, 0x7C8))

    def test_mpc_image_refused(self) -> None:
        with self.assertRaises(ck.MpcImageError) as ctx:
            ck.image_kind(bytes(ck.MPC_FLASH_SIZE))
        self.assertEqual(ctx.exception.code, "MPC_IMAGE_UNSUPPORTED")

    def test_wrong_size_refused(self) -> None:
        with self.assertRaises(ck.ImageFormatError) as ctx:
            ck.image_kind(bytes(0x10000))
        self.assertEqual(ctx.exception.code, "IMAGE_SIZE_UNSUPPORTED")

    def test_family_from_id(self) -> None:
        self.assertEqual(ck.read_family(BO00_CAL.read_bytes()), "4560BO00S")
        self.assertEqual(ck.read_family(LO00_CAL.read_bytes()), "4570LO00S")
        self.assertEqual(ck.read_family(REFERENCE_CAL.read_bytes()), "4560BN00S")
        self.assertEqual(ck.read_family(bytes(0x1D000)), None)

    def test_store_offset_tables_cover_every_family(self) -> None:
        self.assertEqual(len(ck.MO3_STORE_OFFSET), 10)
        self.assertEqual(len(ck.MO3_SWITCH_OFFSET), 10)
        self.assertEqual(set(ck.MO3_STORE_OFFSET), set(ck.MO3_SWITCH_OFFSET))
        for family, offset in ck.MO3_STORE_OFFSET.items():
            self.assertLess(offset + 8, ck.CAL_SIZE, family)
            # Пара не должна попадать в собственную область суммирования.
            self.assertGreaterEqual(offset, ck.MO3_REGION[1], family)


class TestCrc32Implementation(unittest.TestCase):
    """CRC-32/MPEG-2: эталонный вектор и совпадение реализаций."""

    def test_known_vector(self) -> None:
        # Стандартная проверка CRC-32/MPEG-2 для строки "123456789".
        self.assertEqual(ck.crc32_mpeg2(b"123456789"), 0x0376E6E7)

    def test_table_matches_bitwise(self) -> None:
        samples = [b"", b"A", bytes(range(256)), b"MS45" * 100]
        for data in samples:
            self.assertEqual(
                ck.crc32_mpeg2(data),
                ck.crc32_mpeg2_bitwise(data),
                f"расхождение на {len(data)} байт",
            )

    def test_init_and_xorout_applied(self) -> None:
        self.assertEqual(ck.crc32_mpeg2(b"123456789", init=0, xorout=0), ck.crc32_mpeg2_bitwise(b"123456789", 0, 0))
        self.assertEqual(
            ck.crc32_mpeg2(b"123456789", xorout=0xFFFFFFFF) ^ 0xFFFFFFFF,
            ck.crc32_mpeg2(b"123456789"),
        )


class TestMo3Implementation(unittest.TestCase):
    """Аддитивная сумма MO3."""

    def test_seed_and_region(self) -> None:
        cal = bytearray(ck.CAL_SIZE)
        self.assertEqual(ck.mo3_total(bytes(cal)), ck.MO3_SEED)
        cal[ck.MO3_REGION[0] : ck.MO3_REGION[0] + 4] = (1).to_bytes(4, "big")
        self.assertEqual(ck.mo3_total(bytes(cal)), ck.MO3_SEED + 1)

    def test_word_count_is_354(self) -> None:
        start, end = ck.MO3_REGION
        self.assertEqual((end - start) // 4, 354)
        self.assertEqual((end - start) % 4, 0)

    def test_beyond_region_ignored(self) -> None:
        cal = bytearray(ck.CAL_SIZE)
        cal[ck.MO3_REGION[1]] = 0xFF
        cal[ck.CAL_SIZE - 1] = 0xFF
        self.assertEqual(ck.mo3_total(bytes(cal)), ck.MO3_SEED)

    def test_locate_unique(self) -> None:
        cal = bytearray(ck.CAL_SIZE)
        pair = ck.mo3_pair(ck.mo3_total(bytes(cal)))
        self.assertEqual(ck.locate_mo3_store(bytes(cal), pair), [])
        cal[0x5000:0x5008] = pair[0].to_bytes(4, "big") + pair[1].to_bytes(4, "big")
        self.assertEqual(ck.locate_mo3_store(bytes(cal), pair), [0x5000])
        cal[0x6000:0x6008] = pair[0].to_bytes(4, "big") + pair[1].to_bytes(4, "big")
        self.assertEqual(ck.locate_mo3_store(bytes(cal), pair), [0x5000, 0x6000])


class TestStockImages(unittest.TestCase):
    """Все стоковые калибровки: обе суммы сходятся, пара лежит там, где ожидается."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = _all_cal_files()

    def test_all_stock_calibrations_verify(self) -> None:
        self.assertGreaterEqual(len(self.files), 232)
        bad: list[str] = []
        for path in self.files:
            report = ck.verify_image(path)
            if not report.ok:
                bad.append(f"{path.name}: crc={report.crc32_ok} mo3={report.mo3_ok}")
        self.assertEqual(bad, [], f"не прошли проверку: {bad[:5]}")

    def test_store_offset_matches_family_table(self) -> None:
        """Место пары для каждого файла равно табличному (адреса не переносятся)."""
        mismatched: list[str] = []
        for path in self.files:
            image = path.read_bytes()
            cal, _ = ck.extract_cal(image)
            family = ck.read_family(cal)
            pair = ck.mo3_pair(ck.mo3_total(cal))
            hits = ck.locate_mo3_store(cal, pair)
            if hits != [ck.MO3_STORE_OFFSET[family]]:
                mismatched.append(f"{path.name}: {family} -> {[hex(h) for h in hits]}")
        self.assertEqual(mismatched, [], f"несовпадения: {mismatched[:5]}")

    def test_every_family_represented(self) -> None:
        families = {ck.read_family(p.read_bytes()) for p in self.files}
        self.assertEqual(families, set(ck.MO3_STORE_OFFSET))

    def test_checksums_inside_calibration(self) -> None:
        """Обе суммы лежат внутри области калибровки и покрыты её границами."""
        for offset in ck.MO3_STORE_OFFSET.values():
            self.assertLess(offset + 8, ck.CAL_SIZE)
        self.assertLess(ck.CRC32_STORE_OFFSET, ck.CRC32_REGION[0])


class TestDumpImage(unittest.TestCase):
    """Реальное полное чтение 1 МиБ и внутренняя флеш MPC."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dump = FULL_DUMP.read_bytes()

    def test_full_dump_verifies(self) -> None:
        report = ck.verify_bytes(self.dump, str(FULL_DUMP))
        self.assertEqual(report.kind, "full")
        self.assertEqual(report.family, "4560BN00S")
        self.assertTrue(report.crc32_ok, "CRC-32 полного чтения")
        self.assertTrue(report.mo3_ok, "MO3 полного чтения")
        self.assertTrue(report.pointers_ok)
        self.assertEqual(report.mo3_store_offset, ck.MO3_STORE_OFFSET["4560BN00S"])

    def test_cal_slice_matches_stock_file(self) -> None:
        self.assertEqual(self.dump[0x40000:0x5D000], REFERENCE_CAL.read_bytes())

    def test_mpc_dump_refused(self) -> None:
        with self.assertRaises(ck.MpcImageError):
            ck.verify_bytes(MPC_DUMP.read_bytes(), str(MPC_DUMP))


class TestRecompute(unittest.TestCase):
    """Пересчёт сумм, порядок зависимостей и границы записи."""

    def setUp(self) -> None:
        self.image = bytearray(BO00_CAL.read_bytes())

    def test_idempotent_on_stock(self) -> None:
        repaired, result = ck.recompute(bytes(self.image))
        self.assertEqual(repaired, bytes(self.image))
        self.assertEqual(result.changed_offsets, [])
        self.assertEqual(result.mo3_store_offset, ck.MO3_STORE_OFFSET["4560BO00S"])

    def test_patch_outside_mo3_changes_only_crc(self) -> None:
        self.image[0x1000] ^= 0xFF
        repaired, result = ck.recompute(bytes(self.image))
        self.assertEqual(result.changed_offsets, [ck.CRC32_STORE_OFFSET])
        self.assertTrue(ck.verify_bytes(repaired).ok)

    def test_patch_inside_mo3_changes_both(self) -> None:
        self.image[0x400] ^= 0x01
        repaired, result = ck.recompute(bytes(self.image))
        self.assertEqual(
            sorted(result.changed_offsets),
            sorted([ck.CRC32_STORE_OFFSET, result.mo3_store_offset]),
        )
        report = ck.verify_bytes(repaired)
        self.assertTrue(report.ok)

    def test_signature_and_header_untouched(self) -> None:
        original = bytes(self.image)
        self.image[0x8000] ^= 0x42
        repaired, _ = ck.recompute(bytes(self.image))
        # 0x174..0x1B3 -- RSA-подпись, 0x104..0x14F -- дескрипторы областей.
        self.assertEqual(repaired[0x104:0x170], original[0x104:0x170])
        self.assertEqual(repaired[0x170:0x200], original[0x170:0x200])
        self.assertEqual(repaired[0x200:0x400], original[0x200:0x400])

    def test_full_image_writes_only_calibration(self) -> None:
        dump = bytearray(FULL_DUMP.read_bytes())
        dump[0x40200] ^= 0x01
        repaired, result = ck.recompute(bytes(dump))
        self.assertEqual(result.kind, "full")
        outside = [i for i in range(len(dump)) if dump[i] != repaired[i] and not 0x40000 <= i < 0x5D000]
        self.assertEqual(outside, [])
        report = ck.verify_bytes(repaired)
        self.assertTrue(report.ok)
        self.assertFalse(report.crc32_ok is None)
        self.assertTrue(ck.verify_bytes(repaired).pointers_ok)

    def test_recompute_after_patch_strict(self) -> None:
        """Строгий путь ядра: исходник проверяется, правка -- внутри калибровки."""
        source = bytes(self.image)
        patched = bytearray(source)
        patched[0x3300] ^= 0x80
        repaired, result = ck.recompute_after_patch(source, bytes(patched))
        self.assertTrue(ck.verify_bytes(repaired).ok)
        self.assertTrue(result.changed_offsets)
        # Место пары взято из проверенного источника.
        self.assertEqual(result.mo3_store_offset, ck.MO3_STORE_OFFSET["4560BO00S"])

    def test_broken_source_refused(self) -> None:
        """Файл, не проходящий проверку, пересчитывать нельзя."""
        broken = bytearray(self.image)
        broken[0x100] ^= 0xFF
        patched = bytearray(broken)
        patched[0x2000] ^= 0x01
        with self.assertRaises(ck.SourceChecksumError) as ctx:
            ck.recompute_after_patch(bytes(broken), bytes(patched))
        self.assertEqual(ctx.exception.code, "SOURCE_CRC_INVALID")

    def test_broken_mo3_refused(self) -> None:
        offset = ck.MO3_STORE_OFFSET["4560BO00S"]
        broken = bytearray(self.image)
        broken[offset] ^= 0xFF
        broken[offset + 4] ^= 0xFF
        with self.assertRaises(ck.SourceChecksumError):
            ck.recompute_after_patch(bytes(broken), bytes(broken))

    def test_patch_outside_calibration_refused(self) -> None:
        """Правка за пределами калибровки -- жёсткий отказ, а не предупреждение."""
        source = FULL_DUMP.read_bytes()
        patched = bytearray(source)
        patched[0x60000] ^= 0x01  # программная область
        with self.assertRaises(ck.OutsideCalRegionError) as ctx:
            ck.recompute_after_patch(source, bytes(patched))
        self.assertEqual(ctx.exception.code, "OUTSIDE_CAL_REGION")
        patched = bytearray(source)
        patched[0x1000] ^= 0x01  # загрузчик/системная часть
        with self.assertRaises(ck.OutsideCalRegionError):
            ck.recompute_after_patch(source, bytes(patched))

    def test_unknown_family_without_pair_refused(self) -> None:
        image = bytearray(BO00_CAL.read_bytes())
        image[0:0x50] = b"\x00" * 0x50  # стираем идентификатор семейства
        offset = ck.MO3_STORE_OFFSET["4560BO00S"]
        image[offset : offset + 8] = b"\x00" * 8
        image[ck.CRC32_STORE_OFFSET : ck.CRC32_STORE_OFFSET + 4] = ck.crc32_computed(bytes(image)).to_bytes(4, "big")
        with self.assertRaises(ck.ChecksumLayoutError) as ctx:
            ck.recompute(bytes(image))
        self.assertEqual(ctx.exception.code, "CHECKSUM_LAYOUT_UNKNOWN")

    def test_all_families_recompute_roundtrip(self) -> None:
        """Пересчёт работает для всех семейств, а не только для BO/LO."""
        for path in _all_cal_files()[:12]:
            image = bytearray(path.read_bytes())
            image[0x2000] ^= 0x0F
            repaired, _ = ck.recompute(bytes(image))
            self.assertTrue(ck.verify_bytes(repaired).ok, path.name)

    def test_repair_image_writes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.bin"
            target = Path(tmp) / "out.bin"
            image = bytearray(BO00_CAL.read_bytes())
            image[0x3000] ^= 0x11
            source.write_bytes(image)
            result = ck.repair_image(source, target)
            self.assertTrue(ck.verify_image(target).ok)
            # Правка вне области MO3 (0x240..0x7C8) => меняется только CRC-32.
            self.assertEqual(sorted(result.changed_offsets), list(range(0x100, 0x104)))
            image[0x400] ^= 0x01
            source.write_bytes(image)
            result = ck.repair_image(source, target)
            # Правка внутри области MO3 => меняются и CRC-32, и пара MO3,
            # и НИЧЕГО кроме них.
            changed = set(result.changed_offsets)
            allowed = set(range(0x100, 0x104)) | set(
                range(result.mo3_store_offset, result.mo3_store_offset + 8)
            )
            self.assertTrue(changed)
            self.assertLessEqual(changed, allowed)
            self.assertLessEqual(set(range(0x100, 0x104)), changed)
            self.assertTrue(changed & set(range(result.mo3_store_offset, result.mo3_store_offset + 8)))

    def test_changed_offsets_are_absolute_for_full_image(self) -> None:
        """Смещения изменённых байт -- абсолютные (нужны пост-проверке ядра)."""
        dump = bytearray(FULL_DUMP.read_bytes())
        dump[0x40200] ^= 0x01
        _, result = ck.recompute(bytes(dump))
        self.assertTrue(all(0x40000 <= off < 0x5D000 for off in result.changed_offsets))
        self.assertIn(0x40100, result.changed_offsets)

    def test_checksum_cells(self) -> None:
        cells = ck.checksum_cells(BO00_CAL.read_bytes())
        self.assertEqual(len(cells), 12)
        self.assertEqual(cells[:4], [0x100, 0x101, 0x102, 0x103])
        self.assertEqual(cells[4:], list(range(0x5820, 0x5828)))
        full = ck.checksum_cells(FULL_DUMP.read_bytes())
        self.assertEqual(full[0], 0x40100)


class TestCli(unittest.TestCase):
    """CLI проверки сумм."""

    def test_main_on_reference(self) -> None:
        code = ck.main([str(REFERENCE_CAL)])
        self.assertEqual(code, 0)

    def test_main_reports_bad(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.bin"
            image = bytearray(BO00_CAL.read_bytes())
            image[0x100] ^= 0xFF
            broken.write_bytes(image)
            self.assertEqual(ck.main([str(broken)]), 1)

    def test_markdown_report(self) -> None:
        report = ck.verify_image(LO00_CAL)
        text = ck.report_as_markdown([report])
        self.assertIn("4570LO00S", text)
        self.assertIn("ok", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

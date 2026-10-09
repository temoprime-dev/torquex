import tempfile
import unittest
from pathlib import Path

from MS42.tools.analyze_firmware import (
    CALIBRATION_BASE,
    CALIBRATION_SIZE,
    FULL_SIZE,
    OSID,
    PARTIAL_BASE,
    PARTIAL_SIZE,
    READ_32K_SIZE,
    analyze,
    compute_record,
    crc16,
    parse_record,
    verify_record,
)

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "MS42" / "firmware"

#: CRC-16/ARC check value from the catalogue: CRC("123456789", init 0xFFFF) = 0x4B37.
CATALOGUE_CHECK = (b"123456789", 0xFFFF, 0x4B37)


class CrcTests(unittest.TestCase):
    def test_catalogue_check_value(self):
        data, seed, expected = CATALOGUE_CHECK
        self.assertEqual(expected, crc16(data, seed))

    def test_seed_is_applied(self):
        self.assertNotEqual(crc16(b"abc", 0), crc16(b"abc", 0xFFFF))


class AnalyzeFirmwareTests(unittest.TestCase):
    def test_all_ms42_fixtures_are_content_identified_as_siemens(self):
        fixtures = sorted(FIRMWARE.glob("*.bin"))
        self.assertEqual(16, len(fixtures))
        for fixture in fixtures:
            with self.subTest(fixture=fixture.name):
                result = analyze(fixture)
                self.assertTrue(result.supported, result.reasons)
                self.assertEqual("Siemens", result.manufacturer)
                self.assertEqual("MS42", result.ecu_family)
                self.assertEqual("0110C6", result.osid)
                self.assertEqual("full-flash-512K", result.image_type)
                self.assertEqual(CALIBRATION_BASE, result.calibration_offset)
                self.assertEqual(0, result.address_base)
                self.assertTrue(result.crc_valid)
                self.assertEqual({"boot", "cal", "prog"},
                                 {record.name for record in result.crc_records})

    def test_boot_and_program_records_are_identical_across_stock_images(self):
        """Only the calibration (and its CRC word) differs between 0110C6 files."""
        seen = set()
        for fixture in FIRMWARE.glob("*.bin"):
            result = analyze(fixture)
            by_name = {record.name: record.stored for record in result.crc_records}
            seen.add((by_name["boot"], by_name["prog"]))
        self.assertEqual({(0xDF13, 0xF347)}, seen)

    def test_calibration_record_covers_all_objects_and_cal_crc_is_described(self):
        fixture = sorted(FIRMWARE.glob("*.bin"))[0]
        data = fixture.read_bytes()
        result = analyze(fixture)
        cal = next(record for record in result.crc_records if record.name == "cal")
        self.assertEqual([(CALIBRATION_BASE, CALIBRATION_BASE + 0x7843)], cal.segments)
        self.assertEqual(0x7843 + 1, cal.coverage_bytes)
        self.assertEqual(
            int.from_bytes(data[0x4FEE0:0x4FEE2], "little"), cal.stored
        )
        # The descriptor itself sits after the covered region, so it is not
        # protected by its own checksum - the same structure MS41 has.
        self.assertGreater(0x4FEE0, cal.segments[0][1])

    def test_dataset_ids_are_read_from_content(self):
        ids = {}
        for fixture in FIRMWARE.glob("*.bin"):
            ids[fixture.name] = analyze(fixture).dataset_id
        self.assertEqual("91c6322g", ids["Siemens_MS42_0110C6_E46_M52TUB28_EU3_LHD.bin"])
        self.assertEqual("91c6w22g", ids["Siemens_MS42_0110C6_E36Z3_M52TUB20_EU3.bin"])
        # A right-hand-drive image differs from the LHD sibling in that byte only.
        self.assertEqual("91c6322p", ids["Siemens_MS42_0110C6_E46_M52TUB28_EU3_RHD.bin"])
        self.assertEqual(16, len(ids))

    def test_filename_does_not_control_vendor_detection(self):
        source = sorted(FIRMWARE.glob("*.bin"))[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Bosch_fake_name.bin"
            path.write_bytes(source.read_bytes())
            result = analyze(path)
            self.assertTrue(result.supported)
            self.assertEqual("Siemens", result.manufacturer)

    def test_truncated_calibration_crc_is_rejected(self):
        source = sorted(FIRMWARE.glob("*.bin"))[0]
        data = bytearray(source.read_bytes())
        data[0x4FEE0] ^= 0xFF
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tampered.bin"
            path.write_bytes(bytes(data))
            result = analyze(path)
            self.assertFalse(result.supported)
            self.assertFalse(result.crc_valid)
            self.assertTrue(any("cal CRC mismatch" in reason for reason in result.reasons))

    def test_program_code_byte_outside_records_is_not_detected_by_design(self):
        """The program descriptors exclude their own storage; that is documented."""
        source = sorted(FIRMWARE.glob("*.bin"))[0]
        data = bytearray(source.read_bytes())
        # 0x40000..0x47FFF is the 'unused' window and no record covers it.
        self.assertFalse(
            any(start <= 0x40000 <= end
                for start, end in verify_record(data, "prog").segments)
        )
        data[0x40000] ^= 0xFF
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unused_window.bin"
            path.write_bytes(bytes(data))
            self.assertTrue(analyze(path).supported)

    def test_partial_64k_read_is_supported_and_uses_an_address_base(self):
        source = sorted(FIRMWARE.glob("*.bin"))[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial.bin"
            path.write_bytes(source.read_bytes()[PARTIAL_BASE:PARTIAL_BASE + PARTIAL_SIZE])
            result = analyze(path)
            self.assertTrue(result.supported, result.reasons)
            self.assertEqual("partial-64K", result.image_type)
            self.assertEqual(PARTIAL_BASE, result.address_base)
            self.assertEqual(CALIBRATION_BASE - PARTIAL_BASE, result.calibration_offset)
            self.assertEqual(["cal"], [record.name for record in result.crc_records])

    def test_bare_32k_calibration_read_is_supported(self):
        source = sorted(FIRMWARE.glob("*.bin"))[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cal32k.bin"
            path.write_bytes(
                source.read_bytes()[CALIBRATION_BASE:CALIBRATION_BASE + READ_32K_SIZE]
            )
            result = analyze(path)
            self.assertTrue(result.supported, result.reasons)
            self.assertEqual("calibration-32K", result.image_type)
            self.assertEqual(CALIBRATION_BASE, result.address_base)
            self.assertEqual(0, result.calibration_offset)

    def test_random_file_of_the_full_size_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "random.bin"
            path.write_bytes(bytes(FULL_SIZE))
            result = analyze(path)
            self.assertFalse(result.supported)
            self.assertIsNone(result.manufacturer)
            self.assertIsNone(result.ecu_family)
            self.assertIsNone(result.osid)

    def test_wrong_osid_is_rejected(self):
        source = sorted(FIRMWARE.glob("*.bin"))[0]
        data = bytearray(source.read_bytes())
        data[CALIBRATION_BASE + 0x08:CALIBRATION_BASE + 0x0E] = b"0110C7"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong_osid.bin"
            path.write_bytes(bytes(data))
            result = analyze(path)
            self.assertFalse(result.supported)
            self.assertIsNone(result.manufacturer)

    def test_record_is_self_describing(self):
        source = sorted(FIRMWARE.glob("*.bin"))[0]
        data = source.read_bytes()
        stored, segments = parse_record(data, 0x4FEE0)
        self.assertEqual(1, len(segments))
        self.assertEqual((CALIBRATION_BASE, CALIBRATION_BASE + 0x7843), segments[0])
        self.assertEqual(stored, compute_record(data, 0x3643, segments))

    def test_unreadable_descriptor_is_reported_not_crashed(self):
        source = sorted(FIRMWARE.glob("*.bin"))[0]
        data = bytearray(source.read_bytes())
        data[0x4FEE2:0x4FEE4] = (0xFFFF).to_bytes(2, "little")  # implausible count
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken_descriptor.bin"
            path.write_bytes(bytes(data))
            result = analyze(path)
            self.assertFalse(result.supported)
            self.assertTrue(any("unreadable" in reason for reason in result.reasons))

    def test_osid_constant_lives_inside_the_calibration_area(self):
        self.assertEqual(b"0110C6", OSID)


if __name__ == "__main__":
    unittest.main()

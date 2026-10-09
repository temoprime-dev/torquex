import tempfile
import unittest
from pathlib import Path

from MS41.tools.analyze_firmware import analyze


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "MS41" / "firmware"


class AnalyzeFirmwareTests(unittest.TestCase):
    def test_all_ms41_fixtures_are_content_identified_as_siemens(self):
        fixtures = sorted(FIRMWARE.glob("*.bin"))
        self.assertEqual(13, len(fixtures))
        for fixture in fixtures:
            with self.subTest(fixture=fixture.name):
                result = analyze(fixture)
                self.assertTrue(result.supported, result.reasons)
                self.assertEqual("Siemens", result.manufacturer)
                self.assertEqual("MS41", result.ecu_family)
                self.assertEqual("064101", result.software_family)
                self.assertTrue(result.crc_valid)
                self.assertEqual(15, result.crc_blocks)

    def test_full_and_partial_layouts(self):
        for fixture in FIRMWARE.glob("*.bin"):
            result = analyze(fixture)
            if fixture.stat().st_size == 0x6000:
                self.assertEqual("calibration-24K", result.image_type)
                self.assertEqual(0, result.calibration_offset)
            else:
                self.assertEqual("full-flash-256K", result.image_type)
                self.assertEqual(0x14000, result.calibration_offset)

    def test_random_24k_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bosch_named_file.bin"
            path.write_bytes(bytes(0x6000))
            result = analyze(path)
            self.assertFalse(result.supported)
            self.assertIsNone(result.manufacturer)
            self.assertIsNone(result.ecu_family)
            self.assertFalse(result.crc_valid)

    def test_filename_does_not_control_vendor_detection(self):
        source = next(FIRMWARE.glob("*partial*.bin"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Bosch_fake_name.bin"
            path.write_bytes(source.read_bytes())
            result = analyze(path)
            self.assertTrue(result.supported)
            self.assertEqual("Siemens", result.manufacturer)


if __name__ == "__main__":
    unittest.main()

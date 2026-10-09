import unittest
from pathlib import Path

from MS41.tools.validate_xdf_offsets import validate


ROOT = Path(__file__).resolve().parents[2]
DEFINITIONS = ROOT / "MS41" / "definitions"
FIRMWARE = ROOT / "MS41" / "firmware"
PARTIAL_XDF = DEFINITIONS / "Siemens_MS41_1429861_ENG_24K_v1.0.xdf"
FULL_XDF = DEFINITIONS / "Siemens_MS41_1429861_ENG_256K_v1.0.xdf"


class ValidateXdfOffsetsTests(unittest.TestCase):
    def test_xdf_pair_and_all_bins_have_matching_offsets(self):
        report = validate(PARTIAL_XDF, FULL_XDF, sorted(FIRMWARE.glob("*.bin")))
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(0, report["partial_xdf"]["base_offset"])
        self.assertEqual(0x14000, report["full_xdf"]["base_offset"])
        self.assertEqual(
            report["partial_xdf"]["defined_items"],
            report["full_xdf"]["defined_items"],
        )
        self.assertEqual(
            report["partial_xdf"]["addressed_elements"],
            report["full_xdf"]["addressed_elements"],
        )
        self.assertTrue(all(item["in_bounds"] for item in report["bins"]))
        self.assertTrue(all(item["header_matches"] for item in report["bins"]))

    def test_full_address_is_partial_address_plus_0x14000(self):
        report = validate(PARTIAL_XDF, FULL_XDF, [])
        partial = report["partial_xdf"]
        full = report["full_xdf"]
        self.assertEqual(
            partial["minimum_address"] + 0x14000,
            full["base_offset"] + full["minimum_address"],
        )
        self.assertEqual(
            partial["maximum_address"] + 0x14000,
            full["base_offset"] + full["maximum_address"],
        )


if __name__ == "__main__":
    unittest.main()

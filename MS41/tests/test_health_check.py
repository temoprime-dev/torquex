import json
import tempfile
import unittest
from pathlib import Path

from MS41.tools.health_check import REQUIRED_CONFIRMATIONS, evaluate

ROOT = Path(__file__).parents[2]
FIRMWARE = ROOT / "MS41" / "firmware"
KNOWN = FIRMWARE / "MS41_1429861_E36_328i_M52B28_1997_4101_partial.bin"


def good_evidence():
    return {
        "confirmations": {name: True for name in REQUIRED_CONFIRMATIONS},
        "dtcs": {"active": [], "pending": []},
        "fuel_trims_percent": {
            "bank1_ltft": 2.0,
            "bank2_ltft": 1.0,
            "bank1_stft": -1.0,
            "bank2_stft": 0.0,
        },
        "minimum_fuel_ron": 95,
    }


class HealthCheckTests(unittest.TestCase):
    def test_known_healthy_fixture_is_ready(self):
        report = evaluate(KNOWN, good_evidence())
        self.assertEqual(report.status, "READY")
        self.assertNotEqual(report.stock_provenance, "unknown")
        self.assertTrue(report.evidence_complete)

    def test_binary_only_never_claims_ready(self):
        report = evaluate(KNOWN)
        self.assertEqual(report.status, "NEEDS_DIAGNOSIS")
        self.assertFalse(report.evidence_complete)

    def test_active_dtc_or_failed_confirmation_rejects(self):
        evidence = good_evidence()
        evidence["dtcs"]["active"] = ["212 VANOS mechanical"]
        evidence["confirmations"]["vanos_ok"] = False
        report = evaluate(KNOWN, evidence)
        self.assertEqual(report.status, "REJECTED")
        codes = {finding.code for finding in report.findings}
        self.assertIn("DTC_ACTIVE_PRESENT", codes)
        self.assertIn("CONFIRMATION_VANOS_OK", codes)

    def test_excessive_trim_rejects(self):
        evidence = good_evidence()
        evidence["fuel_trims_percent"]["bank1_ltft"] = 15.1
        report = evaluate(KNOWN, evidence)
        self.assertEqual(report.status, "REJECTED")

    def test_valid_unknown_binary_requires_provenance_review(self):
        data = bytearray(KNOWN.read_bytes())
        # Change an identity byte covered by the first block, then update the
        # matching chained CRC to retain a structurally valid calibration.
        data[0x68] ^= 1
        from MS41.tools.analyze_firmware import _crc16
        initial = int.from_bytes(data[0x0E:0x10], "big")
        cursor = 0
        while True:
            checksum_address = int.from_bytes(data[cursor:cursor + 2], "little")
            if cursor <= 0x68 < checksum_address:
                data[checksum_address:checksum_address + 2] = _crc16(
                    data[cursor:checksum_address], initial
                ).to_bytes(2, "little")
                break
            cursor = checksum_address + 2
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unknown.bin"
            path.write_bytes(data)
            report = evaluate(path, good_evidence())
        self.assertEqual(report.status, "NEEDS_DIAGNOSIS")
        self.assertEqual(report.stock_provenance, "unknown")


if __name__ == "__main__":
    unittest.main()

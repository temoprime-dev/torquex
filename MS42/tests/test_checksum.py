import argparse
import hashlib
import json
import random
import tempfile
import unittest
from pathlib import Path

from MS42.tools.analyze_firmware import FirmwareError, _crc16, identify_layout
from MS42.tools.checksum import (
    CRC_SEGMENTS,
    inspect,
    present_records,
    repair,
    run,
)

ROOT = Path(__file__).parents[2]
FIRMWARE = ROOT / "MS42" / "firmware"
FILES = sorted(FIRMWARE.glob("*.bin"))


class StockImageTests(unittest.TestCase):
    """The acceptance criterion: every stock image verifies, byte-exactly."""

    def test_sixteen_files_and_fifteen_distinct(self):
        self.assertEqual(16, len(FILES))
        digests = {hashlib.sha256(path.read_bytes()).hexdigest() for path in FILES}
        self.assertEqual(15, len(digests))

    def test_every_stock_image_verifies(self):
        for path in FILES:
            with self.subTest(path=path.name):
                result = inspect(path)
                self.assertEqual("full-flash-512K", result.image_type)
                self.assertEqual(3, len(result.records))
                self.assertTrue(result.valid_before,
                                [f["message"] for f in result.findings])
                self.assertTrue(result.valid_after)

    def test_boot_and_program_checksums_are_global_constants(self):
        for path in FILES:
            with self.subTest(path=path.name):
                records = {record.name: record for record in inspect(path).records}
                self.assertEqual(0xDF13, records["boot"].stored_before)
                self.assertEqual(0xDF13, records["boot"].computed)
                self.assertEqual(0xF347, records["prog"].stored_before)
                self.assertEqual(0xF347, records["prog"].computed)

    def test_declared_segments_match_the_documented_geometry(self):
        for path in FILES:
            with self.subTest(path=path.name):
                records = {record.name: record for record in inspect(path).records}
                for name, expected in CRC_SEGMENTS.items():
                    self.assertEqual(list(expected), records[name].segments)
                self.assertEqual(30788, records["cal"].coverage_bytes)
                self.assertEqual(15396, records["boot"].coverage_bytes)
                self.assertEqual(267972, records["prog"].coverage_bytes)

    def test_calibration_checksum_is_per_image(self):
        values = set()
        for path in FILES:
            records = {record.name: record for record in inspect(path).records}
            values.add(records["cal"].stored_before)
        # Two images are byte-identical, so at most 15 distinct values, but the
        # point is that it is not one global constant like boot/prog.
        self.assertGreater(len(values), 1)
        self.assertLessEqual(len(values), 15)

    def test_verification_is_independent_of_the_filename(self):
        source = FILES[0]
        with tempfile.TemporaryDirectory() as directory:
            copies = []
            for name in ("a.bin", "renamed_original_read.bin", "copy of a read (1).bin"):
                target = Path(directory) / name
                target.write_bytes(source.read_bytes())
                copies.append(target)
            reference = inspect(source)
            for target in copies[1:]:
                candidate = inspect(target)
                self.assertTrue(candidate.valid_before)
                self.assertEqual(
                    [record.stored_before for record in reference.records],
                    [record.stored_before for record in candidate.records],
                )


class AlgorithmTests(unittest.TestCase):
    def test_bitwise_and_table_driven_crc_agree(self):
        rng = random.Random(42)
        for _ in range(200):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 64)))
            seed = rng.randrange(0x10000)
            self.assertEqual(_crc16(data, seed), _crc16(data, seed))
            from MS42.tools.analyze_firmware import crc16
            self.assertEqual(_crc16(data, seed), crc16(data, seed))

    def test_seed_is_masked_to_16_bits(self):
        from MS42.tools.analyze_firmware import crc16
        self.assertEqual(crc16(b"\x00", 0x12345), crc16(b"\x00", 0x2345))

    def test_known_boot_segment_reproduces_the_stored_value(self):
        from MS42.tools.analyze_firmware import compute_record
        data = FILES[0].read_bytes()
        self.assertEqual(0xDF13, compute_record(data, 0x2D2D, list(CRC_SEGMENTS["boot"]), 0))
        self.assertEqual(0xF347, compute_record(data, 0x3030, list(CRC_SEGMENTS["prog"]), 0))


class RepairTests(unittest.TestCase):
    def test_repairing_every_record_round_trips_byte_exactly(self):
        data = bytearray(FILES[0].read_bytes())
        for address in (0x3C24, 0x4FEE0, 0x50306):
            data[address] ^= 0x5A
        repaired, result = repair(bytes(data), "full-flash-512K")
        self.assertTrue(result.repaired)
        self.assertEqual(2 * 3, result.bytes_written)
        self.assertEqual(FILES[0].read_bytes(), bytes(repaired))
        self.assertTrue(result.valid_after)
        self.assertEqual(hashlib.sha256(FILES[0].read_bytes()).hexdigest(),
                         result.sha256_after)

    def test_content_change_in_the_calibration_is_covered_by_a_crc_repair(self):
        data = bytearray(FILES[0].read_bytes())
        data[0x48000 + 0x10] ^= 0xFF          # inside the cal segment
        repaired, result = repair(bytes(data), "full-flash-512K")
        self.assertTrue(result.valid_after)
        self.assertNotEqual(result.sha256_before, result.sha256_after)
        self.assertEqual(0x48000 + 0x10, next(
            index for index, (before, after) in enumerate(zip(FILES[0].read_bytes(), repaired))
            if before != after and index != 0x4FEE0 and index != 0x4FEE1
        ))
        # Exactly one record changed: the calibration one.
        changed = [record.name for record in result.records if record.changed]
        self.assertEqual(["cal"], changed)

    def test_a_broken_segment_table_is_refused_not_reinterpreted(self):
        data = bytearray(FILES[0].read_bytes())
        data[0x4FEE2:0x4FEE4] = (0).to_bytes(2, "little")   # cal record: zero segments
        with self.assertRaises(FirmwareError):
            repair(bytes(data), "full-flash-512K")

    def test_a_truncated_record_is_refused(self):
        data = bytearray(FILES[0].read_bytes())
        data[0x50306 + 2:0x50306 + 4] = (64).to_bytes(2, "little")  # count too high
        with self.assertRaises(FirmwareError):
            repair(bytes(data), "full-flash-512K")

    def test_repair_never_writes_a_file_when_it_refuses(self):
        data = bytearray(FILES[0].read_bytes())
        data[0x4FEE2:0x4FEE4] = (0).to_bytes(2, "little")
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad.bin"
            bad.write_bytes(data)
            out = Path(directory) / "out.bin"
            code = run(argparse.Namespace(bin=bad, out=out, report=None))
            self.assertEqual(4, code)
            self.assertFalse(out.exists())

    def test_verify_only_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad.bin"
            data = bytearray(FILES[0].read_bytes())
            data[0x4FEE0] ^= 0x01
            bad.write_bytes(data)
            report = Path(directory) / "report.json"
            self.assertEqual(3, run(argparse.Namespace(bin=bad, out=None, report=report)))
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(payload["valid_before"])
            self.assertEqual("CRC_MISMATCH", payload["findings"][0]["code"])
            self.assertEqual(0, run(argparse.Namespace(bin=FILES[0], out=None,
                                                       report=None)))

    def test_unsupported_size_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "short.bin"
            path.write_bytes(b"\x00" * 1024)
            self.assertEqual(1, run(argparse.Namespace(bin=path, out=None, report=None)))
            with self.assertRaises(FirmwareError):
                repair(b"\x00" * 1024, "full-flash-512K")

    def test_repair_writes_a_report_and_the_image(self):
        with tempfile.TemporaryDirectory() as directory:
            data = bytearray(FILES[0].read_bytes())
            data[0x4FEE0] ^= 0x01
            bad = Path(directory) / "bad.bin"
            bad.write_bytes(data)
            out = Path(directory) / "fixed.bin"
            report = Path(directory) / "report.json"
            self.assertEqual(0, run(argparse.Namespace(bin=bad, out=out, report=report)))
            self.assertEqual(FILES[0].read_bytes(), out.read_bytes())
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(payload["repaired"])
            self.assertTrue(payload["valid_after"])
            self.assertEqual(2, payload["bytes_written"])


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.full = FILES[0].read_bytes()

    def test_partial_64k_read_verifies_its_calibration_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial.bin"
            path.write_bytes(self.full[0x40000:0x50000])
            result = inspect(path)
            self.assertEqual("partial-64K", result.image_type)
            self.assertEqual(("cal",), tuple(record.name for record in result.records))
            self.assertTrue(result.valid_before)

    def test_bare_32k_calibration_read_verifies_its_calibration_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cal.bin"
            path.write_bytes(self.full[0x48000:0x50000])
            result = inspect(path)
            self.assertEqual("calibration-32K", result.image_type)
            self.assertEqual(0x48000, result.address_base)
            self.assertTrue(result.valid_before)

    def test_repairing_a_32k_read_fixes_only_its_calibration_crc(self):
        data = bytearray(self.full[0x48000:0x50000])
        data[0x4FEE0 - 0x48000] ^= 0x01
        repaired, result = repair(bytes(data), "calibration-32K")
        self.assertTrue(result.valid_after)
        self.assertEqual(self.full[0x48000:0x50000], bytes(repaired))
        self.assertEqual(2, result.bytes_written)

    def test_present_records_follow_the_layout(self):
        self.assertEqual(("boot", "cal", "prog"), present_records("full-flash-512K"))
        self.assertEqual(("cal",), present_records("partial-64K"))
        self.assertEqual(("cal",), present_records("calibration-32K"))
        self.assertEqual((), present_records("unknown"))


class GeometryTests(unittest.TestCase):
    def test_documented_geometry_is_the_validated_one(self):
        self.assertEqual(((0x00000, 0x03C23),), CRC_SEGMENTS["boot"])
        self.assertEqual(((0x48000, 0x4F843),), CRC_SEGMENTS["cal"])
        self.assertEqual(7, len(CRC_SEGMENTS["prog"]))

    def test_calibration_coverage_ends_where_the_record_says(self):
        # The patch core relies on this: a calibration edit must be inside
        # 0x48000..0x4F843 or it would need a program-record rewrite.
        start, end = CRC_SEGMENTS["cal"][0]
        self.assertEqual(0x48000, start)
        self.assertLess(end, 0x4FEE0)   # the record itself is outside its coverage
        self.assertLess(end, 0x50306)


if __name__ == "__main__":
    unittest.main()

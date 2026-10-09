"""The MS43->MS42 donor transfer: base-offset proof, guard rails, provenance."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from MS42.tools.calibration import DEFAULT_POLICY, DEFAULT_XDF, Policy, PatchError, load_calibration
from MS42.tools.donor import (
    DEFAULT_DONOR_BIN,
    DEFAULT_DONOR_XDF,
    DEFAULT_MAP,
    Donor,
    DonorError,
    inspect_transfer,
    load_donor,
    load_map,
    plan_transfer,
    transposed_order,
)
from MS42.tools.xdf import get_object, load_definition, read_engineering

ROOT = Path(__file__).parents[2]
MS42_FIRMWARE = ROOT / "MS42" / "firmware"
B28_LHD = MS42_FIRMWARE / "Siemens_MS42_0110C6_E46_M52TUB28_EU3_LHD.bin"


def target_objects():
    objects, _problems = load_definition(DEFAULT_XDF)
    return objects


class DonorLoadingTests(unittest.TestCase):
    def test_the_repository_donor_loads_and_validates(self):
        donor = load_donor()
        self.assertEqual("MS43", donor.family)
        self.assertEqual(0x70000, donor.calibration_base)
        self.assertEqual("430069", donor.osid)
        self.assertEqual(512 * 1024, len(donor.image))
        self.assertEqual(hashlib.sha256(donor.image).hexdigest(), donor.sha256)
        self.assertEqual(Path(DEFAULT_DONOR_BIN), Path(donor.path))

    def test_the_base_offset_is_read_from_the_definition(self):
        donor = load_donor()
        text = Path(DEFAULT_DONOR_XDF).read_text(encoding="utf-8", errors="replace")
        self.assertIn(f'offset="0x{donor.calibration_base:X}"', text)

    def test_a_wrong_base_offset_is_refused_not_guessed(self):
        # Shift the declared base: the validator must fail loudly instead of
        # reading a plausible-looking wrong table.
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            xdf = directory / "broken.xdf"
            text = Path(DEFAULT_DONOR_XDF).read_text(encoding="utf-8", errors="replace")
            xdf.write_text(text.replace('offset="0x70000"', 'offset="0x70400"', 1),
                           encoding="utf-8")
            with self.assertRaises(DonorError) as caught:
                load_donor(DEFAULT_DONOR_BIN, xdf)
            self.assertIn("does not produce physically plausible values", str(caught.exception))

    def test_a_definition_without_a_base_offset_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            xdf = Path(directory) / "nobase.xdf"
            text = Path(DEFAULT_DONOR_XDF).read_text(encoding="utf-8", errors="replace")
            xdf.write_text(text.replace("<BASEOFFSET", "<BASEOFFSETXX", 1), encoding="utf-8")
            with self.assertRaises(DonorError):
                load_donor(DEFAULT_DONOR_BIN, xdf)

    def test_a_truncated_donor_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            tiny = Path(directory) / "tiny.bin"
            tiny.write_bytes(DEFAULT_DONOR_BIN.read_bytes()[:0x20000])
            with self.assertRaises(DonorError):
                load_donor(tiny)

    def test_the_definition_problems_are_kept_not_hidden(self):
        donor = load_donor()
        self.assertTrue(donor.problems)
        self.assertTrue(any("X000" in problem or "X00" in problem
                            for problem in donor.problems))

    def test_the_values_read_as_engine_data(self):
        donor = load_donor()
        maf = donor.values("id_maf_tab__v_maf_1__v_maf_2")
        self.assertEqual(256, len(maf))
        self.assertGreater(max(maf), 500)
        self.assertTrue(all(right >= left for left, right in zip(maf, maf[1:])))


class MappingCatalogueTests(unittest.TestCase):
    def test_every_mapping_is_explicit_and_carries_a_note(self):
        transfers = load_map()
        self.assertGreaterEqual(len(transfers), 5)
        for transfer in transfers:
            self.assertTrue(transfer.ms42_symbol)
            self.assertTrue(transfer.ms43_symbol)
            self.assertTrue(transfer.note.strip())
            self.assertIn(transfer.mode, ("direct", "reference"))
            self.assertTrue(transfer.sources)

    def test_the_instructive_scale_mismatch_is_present(self):
        symbols = {transfer.ms42_symbol for transfer in load_map()}
        self.assertIn("c_maf_hys_vim", symbols)

    def test_a_mapping_without_a_note_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            path.write_text(json.dumps({
                "schema": 1,
                "transfers": [{"ms42_symbol": "c_n_hys_vim", "ms43_symbol": "c_n_hys_vim",
                               "mode": "direct"}],
            }), encoding="utf-8")
            with self.assertRaises(DonorError):
                load_map(path)

    def test_an_unknown_mode_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            path.write_text(json.dumps({
                "schema": 1,
                "transfers": [{"ms42_symbol": "c_n_hys_vim", "ms43_symbol": "c_n_hys_vim",
                               "mode": "similar_name", "note": "x"}],
            }), encoding="utf-8")
            with self.assertRaises(DonorError):
                load_map(path)

    def test_an_old_schema_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            path.write_text(json.dumps({"schema": 0, "transfers": []}), encoding="utf-8")
            with self.assertRaises(DonorError):
                load_map(path)


class GuardRailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.donor = load_donor()
        cls.objects = target_objects()
        cls.policy = Policy.load(DEFAULT_POLICY)

    def report(self, symbol):
        transfer = next(item for item in load_map() if item.ms42_symbol == symbol)
        return inspect_transfer(transfer, self.donor, self.objects, self.policy)

    def test_a_shape_and_scale_match_is_accepted(self):
        report = self.report("id_vim_pl__n_vim__maf")
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(48, report["points"])
        self.assertEqual(8, report["rows"])
        self.assertEqual(6, report["columns"])

    def test_a_scale_mismatch_is_refused_with_the_numbers(self):
        report = self.report("c_maf_hys_vim")
        self.assertFalse(report["ok"])
        text = " ".join(report["problems"])
        self.assertIn("scale mismatch", text)
        self.assertIn("5.447059", text)
        self.assertIn("10.8941", text)

    def test_a_symbol_outside_the_policy_is_refused(self):
        policy = Policy.load(DEFAULT_POLICY)
        policy.entries.pop("c_n_hys_vim")
        transfer = next(item for item in load_map() if item.ms42_symbol == "c_n_hys_vim")
        report = inspect_transfer(transfer, self.donor, self.objects, policy)
        self.assertFalse(report["ok"])
        self.assertTrue(any("not in the patch policy" in problem for problem in report["problems"]))

    def test_an_unknown_symbol_is_refused(self):
        report = inspect_transfer(
            type(next(item for item in load_map()))(
                ms42_symbol="c_n_hys_vim", ms43_symbol="does_not_exist",
                mode="direct", note="typo"),
            self.donor, self.objects, self.policy)
        self.assertFalse(report["ok"])
        self.assertTrue(any("does_not_exist" in problem for problem in report["problems"]))

    def test_a_scale_factor_difference_below_the_tolerance_is_accepted(self):
        # id_pvs_fl_vim__n_vim: 0.468627 against 0.46862745 in the two XDFs.
        report = self.report("id_pvs_fl_vim__n_vim")
        self.assertTrue(report["ok"], report["problems"])

    def test_a_policy_violation_is_refused(self):
        policy = Policy.load(DEFAULT_POLICY)
        entry = policy.entry("c_n_hys_vim")
        self.assertIsNotNone(entry)
        # Pretend the policy bound is 20 rpm; the donor holds 30.
        import dataclasses
        policy.entries["c_n_hys_vim"] = dataclasses.replace(entry, maximum=20.0)
        transfer = next(item for item in load_map() if item.ms42_symbol == "c_n_hys_vim")
        report = inspect_transfer(transfer, self.donor, self.objects, policy)
        self.assertFalse(report["ok"])
        self.assertTrue(any("above the policy maximum" in problem
                            for problem in report["problems"]))

    def test_transposed_order_is_a_real_transpose(self):
        self.assertEqual([0, 3, 1, 4, 2, 5], transposed_order(2, 3))
        # A 1x8 row and an 8x1 column hold the same sequence in flat order.
        self.assertEqual(list(range(8)), transposed_order(8, 1))

    def test_a_shape_mismatch_without_a_declared_transpose_is_refused(self):
        report = inspect_transfer(
            type(next(item for item in load_map()))(
                ms42_symbol="id_vim_fl__n_vim", ms43_symbol="c_n_hys_vim",
                mode="direct", note="shape test"),
            self.donor, self.objects, self.policy)
        self.assertFalse(report["ok"])
        self.assertTrue(any("shape mismatch" in problem for problem in report["problems"]))


class PlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.donor = load_donor()
        cls.objects = target_objects()
        cls.policy = Policy.load(DEFAULT_POLICY)
        image, offset, _type, _base = load_calibration(B28_LHD)
        cls.image = image
        cls.offset = offset

    def plan(self, **kwargs):
        return plan_transfer(self.donor, self.objects, self.image, self.offset,
                             self.policy, **kwargs)

    def test_the_direct_mappings_apply_and_the_rest_are_reported(self):
        changes, reports = self.plan()
        applied = {report["ms42_symbol"] for report in reports if report["applied"]}
        self.assertIn("id_vim_pl__n_vim__maf", applied)
        self.assertIn("id_vim_fl__n_vim", applied)
        self.assertIn("c_n_hys_vim", applied)
        self.assertNotIn("c_maf_hys_vim", applied)      # scale mismatch
        self.assertNotIn("c_maf_max_diag", applied)     # reference value
        self.assertEqual(sum(1 for report in reports if report["applied"]), len(applied))

    def test_the_reference_value_goes_in_only_with_an_explicit_opt_in(self):
        changes, reports = self.plan(include_references=True)
        applied = {report["ms42_symbol"] for report in reports if report["applied"]}
        self.assertIn("c_maf_max_diag", applied)
        reference = [change for change in changes if change["symbol"] == "c_maf_max_diag"]
        self.assertEqual(1, len(reference))
        self.assertEqual(890.0, reference[0]["value"])

    def test_every_change_carries_the_donor_provenance(self):
        changes, _reports = self.plan()
        self.assertTrue(changes)
        for change in changes:
            self.assertTrue(change["evidence"].startswith("donor_ms43:"))
            self.assertIn(self.donor.label, change["evidence"])
            self.assertIn(change["donor_symbol"], change["evidence"])
            self.assertTrue(change["reason"])
            self.assertTrue(change["requires_validation"])
            self.assertTrue(change["validation_hint"])
            self.assertIn("donor_symbol", change)

    def test_the_written_values_equal_the_donor_values(self):
        changes, _reports = self.plan()
        for change in changes:
            expected = self.donor.values(change["donor_symbol"],
                                         "transposed"
                                         if next(item for item in load_map()
                                                 if item.ms42_symbol == change["symbol"]
                                                 ).orientation == "transposed" else "same")
            self.assertEqual(expected[change["index"]], change["value"])

    def test_a_plan_without_a_donor_is_not_possible(self):
        with self.assertRaises(DonorError):
            plan_transfer(None, self.objects, self.image, self.offset, self.policy)

    def test_the_count_of_changed_cells_matches_the_applied_tables(self):
        changes, reports = self.plan()
        expected = sum(report["points"] for report in reports if report["applied"])
        self.assertEqual(expected, len(changes))


if __name__ == "__main__":
    unittest.main()

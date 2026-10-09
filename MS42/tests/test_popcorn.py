"""Popcorn profiles: what the tool offers, what it refuses, what it writes."""

import json
import tempfile
import unittest
from pathlib import Path

from MS42.tools.calibration import DEFAULT_POLICY, DEFAULT_XDF, Policy, PatchError, load_calibration
from MS42.tools.popcorn import (
    DELAY_SYMBOL,
    IGNITION_SYMBOLS,
    INTENSITY_BANDS,
    REFUSED_INTENSITY,
    THRESHOLD_SYMBOLS,
    PopcornProfile,
    achieved_raises,
    load_profile,
    parse_profile,
    popcorn_changes,
    requirements,
    snap_to_grid,
)
from MS42.tools.xdf import get_object, load_definition, read_engineering

ROOT = Path(__file__).parents[2]
FIRMWARE = ROOT / "MS42" / "firmware"
B28_LHD = FIRMWARE / "Siemens_MS42_0110C6_E46_M52TUB28_EU3_LHD.bin"
EXAMPLES = ROOT / "MS42" / "examples"

NO_CATALYST = "Письменно: катализатора нет, машина используется вне дорог общего пользования."
ROAD_PLAN = "Первый заезд 10 минут, 2-3 сброса газа, наблюдатель следит за температурой выпуска."


def profile(**overrides) -> dict:
    payload = {
        "kind": "popcorn",
        "no_catalyst": NO_CATALYST,
        "thermal_watch": True,
        "road_validation_plan": ROAD_PLAN,
        "intensity": "light",
        "threshold_add_rpm": 400,
        "retard_deg": -4.5,
        "cut_delay_s": 0.8,
    }
    payload.update(overrides)
    return payload


class ProfileGateTests(unittest.TestCase):
    def test_a_complete_profile_parses(self):
        parsed = parse_profile(profile())
        self.assertEqual("light", parsed.intensity)
        self.assertEqual(400, parsed.threshold_add_rpm)
        self.assertIn("катализатора нет", parsed.no_catalyst)
        self.assertEqual(ROAD_PLAN, parsed.road_validation_plan)

    def test_a_written_no_catalyst_statement_is_required(self):
        for value in (None, "", "нет", "нет кат"):
            with self.subTest(value=value):
                with self.assertRaises(PatchError):
                    parse_profile(profile(no_catalyst=value))

    def test_the_thermal_acknowledgement_is_required(self):
        with self.assertRaises(PatchError):
            parse_profile(profile(thermal_watch=False))
        with self.assertRaises(PatchError):
            parse_profile(profile(thermal_watch=None))

    def test_a_road_validation_plan_is_required(self):
        for value in (None, "", "ок"):
            with self.subTest(value=value):
                with self.assertRaises(PatchError):
                    parse_profile(profile(road_validation_plan=value))

    def test_an_aggressive_preset_is_refused_by_name(self):
        for name in REFUSED_INTENSITY:
            with self.subTest(intensity=name):
                with self.assertRaises(PatchError):
                    parse_profile(profile(intensity=name))

    def test_an_unknown_intensity_is_refused(self):
        with self.assertRaises(PatchError):
            parse_profile(profile(intensity="ludicrous"))

    def test_band_bounds_are_enforced(self):
        cases = (
            {"threshold_add_rpm": 601},          # light band
            {"threshold_add_rpm": -1},
            {"retard_deg": -6.1},
            {"retard_deg": 0.1},
            {"cut_delay_s": 1.3},
            {"cut_delay_s": -0.1},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(PatchError):
                    parse_profile(profile(**overrides))

    def test_the_medium_band_is_wider_but_still_bounded(self):
        medium = parse_profile(profile(intensity="medium", threshold_add_rpm=1800,
                                       retard_deg=-10, cut_delay_s=2.5))
        self.assertEqual(1800, medium.threshold_add_rpm)
        for overrides in ({"retard_deg": -12.1}, {"cut_delay_s": 3.1},
                          {"threshold_add_rpm": 2001}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(PatchError):
                    parse_profile(profile(intensity="medium", **overrides))

    def test_numbers_must_be_numbers(self):
        for field in ("threshold_add_rpm", "retard_deg", "cut_delay_s"):
            with self.subTest(field=field):
                with self.assertRaises(PatchError):
                    parse_profile(profile(**{field: "much"}))

    def test_the_kind_field_is_checked(self):
        with self.assertRaises(PatchError):
            parse_profile(profile(kind="tune"))

    def test_requirements_name_the_tool_and_the_fields(self):
        payload = requirements()
        self.assertEqual("popcorn", payload["kind"])
        text = " ".join(payload["fields"])
        self.assertIn("no_catalyst", text)
        self.assertIn("road_validation_plan", text)
        self.assertIn("light", text)
        self.assertEqual("MS42/examples/popcorn.example.json", payload["example"])
        self.assertIn("агрессивный публичный пресет", payload["refused"])

    def test_the_example_profile_is_valid(self):
        parsed = load_profile(EXAMPLES / "popcorn.example.json")
        self.assertEqual("light", parsed.intensity)
        self.assertTrue(parsed.road_validation_plan)


class GridTests(unittest.TestCase):
    def test_snapping_uses_the_definition_step(self):
        self.assertEqual(1568.0, snap_to_grid(1552.0, 32.0))   # 48.5 -> up, not banker's
        self.assertEqual(1536.0, snap_to_grid(1551.0, 32.0))
        self.assertEqual(1568.0, snap_to_grid(1553.0, 32.0))
        self.assertEqual(-4.5, snap_to_grid(-4.5, 0.375))
        self.assertEqual(-4.875, snap_to_grid(-5.0, 0.375))
        self.assertEqual(7.5, snap_to_grid(7.5, 0))

    def test_every_written_value_lands_on_the_raw_grid(self):
        image, offset, _type, _base = load_calibration(B28_LHD)
        objects, _ = load_definition(DEFAULT_XDF)
        parsed = parse_profile(profile())
        for change in popcorn_changes(parsed, image, offset, objects):
            obj = get_object(objects, change["symbol"])
            steps = change["value"] / obj.scaler.a
            self.assertAlmostEqual(round(steps), steps, places=6,
                                   msg=f"{change['symbol']}[{change['index']}] off grid")


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.image, self.offset, _type, _base = load_calibration(B28_LHD)
        self.objects, _ = load_definition(DEFAULT_XDF)
        self.profile = parse_profile(profile())

    def test_both_the_ac_off_and_the_ac_on_families_are_written(self):
        changes = popcorn_changes(self.profile, self.image, self.offset, self.objects)
        symbols = {change["symbol"] for change in changes}
        for symbol in THRESHOLD_SYMBOLS + IGNITION_SYMBOLS + (DELAY_SYMBOL,):
            self.assertIn(symbol, symbols)
        self.assertEqual(len(changes), 6 + 6 + 24 + 24 + 24)

    def test_the_threshold_is_raised_and_the_ignition_is_retarded(self):
        changes = popcorn_changes(self.profile, self.image, self.offset, self.objects)
        calibration = bytes(self.image[self.offset:self.offset + 0x8000])
        for change in changes:
            obj = get_object(self.objects, change["symbol"])
            stock = read_engineering(calibration, obj)[change["index"]]
            if change["symbol"] in THRESHOLD_SYMBOLS:
                self.assertAlmostEqual(400, change["value"] - stock, delta=16)
            elif change["symbol"] in IGNITION_SYMBOLS:
                self.assertLess(change["value"], stock + 0.001)
                self.assertGreater(change["value"], stock - 4.6)
            else:
                self.assertEqual(0.8, change["value"])

    def test_the_delay_table_takes_one_value_everywhere(self):
        changes = [change for change in popcorn_changes(
            self.profile, self.image, self.offset, self.objects)
            if change["symbol"] == DELAY_SYMBOL]
        self.assertEqual(24, len(changes))
        self.assertEqual({0.8}, {change["value"] for change in changes})

    def test_every_change_quotes_the_customer_statement(self):
        for change in popcorn_changes(self.profile, self.image, self.offset, self.objects):
            self.assertTrue(change["evidence"].startswith("customer_intent:no_catalyst="))
            self.assertTrue(change["requires_validation"])
            self.assertTrue(change["validation_hint"])

    def test_the_reported_units_are_the_physical_ones(self):
        changes = popcorn_changes(self.profile, self.image, self.offset, self.objects)
        units = {change["symbol"]: change["units"] for change in changes}
        self.assertEqual("[1/min]", units["ip_n_min_puc__tco"])
        self.assertEqual("[1/min]", units["ip_n_min_accin_puc__tco"])
        self.assertEqual("s", units["ip_t_puc_deacc__n__maf_mmv"])

    def test_achieved_raises_stay_within_one_grid_step(self):
        achieved = achieved_raises(self.profile, self.image, self.offset, self.objects)
        self.assertGreaterEqual(achieved["min_rpm"], 400 - 16)
        self.assertLessEqual(achieved["max_rpm"], 400 + 16)

    def test_the_written_values_pass_the_real_policy(self):
        policy = Policy.load(DEFAULT_POLICY)
        calibration = bytes(self.image[self.offset:self.offset + 0x8000])
        for change in popcorn_changes(self.profile, self.image, self.offset, self.objects):
            obj = get_object(self.objects, change["symbol"])
            entry = policy.entry(change["symbol"])
            self.assertIsNotNone(entry, change["symbol"])
            self.assertGreaterEqual(change["value"], entry.minimum)
            self.assertLessEqual(change["value"], entry.maximum)
            self.assertLessEqual(len(read_engineering(calibration, obj)),
                                 entry.max_cells_per_patch)

    def test_the_bands_table_is_small_and_documented(self):
        self.assertEqual({"light", "medium"}, set(INTENSITY_BANDS))
        self.assertEqual((600.0, 6.0, 1.2), INTENSITY_BANDS["light"])
        self.assertEqual((2000.0, 12.0, 3.0), INTENSITY_BANDS["medium"])


class FileLoadingTests(unittest.TestCase):
    def test_a_profile_loads_from_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "popcorn.json"
            path.write_text(json.dumps(profile()), encoding="utf-8")
            parsed = load_profile(path)
            self.assertIsInstance(parsed, PopcornProfile)
            payload = parsed.to_json()
            self.assertEqual("light", payload["intensity"])
            self.assertEqual(ROAD_PLAN, payload["road_validation_plan"])
            self.assertTrue(payload["thermal_watch"])


if __name__ == "__main__":
    unittest.main()

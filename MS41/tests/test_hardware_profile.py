import json
import unittest
from pathlib import Path

from MS41.tools.hardware_profile import (
    CALIBRATION_WORK,
    DEFAULT_XDF,
    ENGINES,
    HardwareProfile,
    evaluate,
    load_xdf_symbols,
    parse_profile,
)

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "MS41" / "examples"


def base_profile(**overrides):
    profile = HardwareProfile(
        engine="m52b28",
        dme_variant="ms411",
        transmission="manual",
        manifold="stock_m52",
        cams="stock",
        exhaust="stock",
        intake="stock_airbox",
        maf="stock_hfm",
        injectors="stock",
        catalyst="present",
        fuel_ron=95,
    )
    for key, value in overrides.items():
        setattr(profile, key, value)
    return profile


class XdfSymbolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.symbols = load_xdf_symbols()

    def test_every_referenced_calibration_object_exists(self):
        missing = []
        for key, entry in CALIBRATION_WORK.items():
            for name in entry["symbols"]:
                if name not in self.symbols:
                    missing.append((key, name))
        self.assertEqual(missing, [], f"work order references unknown XDF objects: {missing}")

    def test_symbols_have_addresses_and_sizes(self):
        for name, entries in self.symbols.items():
            for entry in entries:
                self.assertTrue(entry["address"].startswith("0x"), name)
                self.assertIsNotNone(entry["element_size_bits"], name)

    def test_known_vanos_tables_differ_by_transmission(self):
        manual = self.symbols["ip_maf_min_ivvt__n__tco"]
        automatic = self.symbols["ip_dri_maf_min_ivvt__n__tco"]
        self.assertNotEqual(manual[0]["address"], automatic[0]["address"])

    def test_table_data_address_is_separate_from_the_header(self):
        fuel_cut = self.symbols["ip_n_min_puc__tco"][0]
        self.assertEqual(fuel_cut["address"], "0x1C44")
        self.assertEqual(fuel_cut["data_addresses"], ["0x1D66"])

    def test_ac_variant_shares_header_but_not_data(self):
        normal = self.symbols["ip_n_min_puc__tco"][0]
        with_ac = self.symbols["ip_n_min_accin_puc__tco"][0]
        self.assertEqual(normal["address"], with_ac["address"])
        self.assertNotEqual(normal["data_addresses"], with_ac["data_addresses"])

    def test_deceleration_ignition_data_matches_the_known_range(self):
        decel = self.symbols["ip_iga_puc__n"][0]
        self.assertEqual(decel["address"], "0x14BE")
        self.assertEqual(decel["data_addresses"], ["0x18E4"])


class ProfileParsingTests(unittest.TestCase):
    def test_example_profile_parses(self):
        data = json.loads((EXAMPLES / "stage2_profile.example.json").read_text())
        profile = parse_profile(data)
        self.assertEqual(profile.engine, "m52b28")
        self.assertEqual(profile.fuel_ron, 95)

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_profile({"engine": "m52b28", "turbo": True})

    def test_non_boolean_option_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_profile({"hardcut": "yes"})


class Stage2DecisionTests(unittest.TestCase):
    def test_stock_profile_is_stage1_without_work(self):
        report = evaluate(base_profile(), {}, stage2_requested=False)
        self.assertEqual(report.stage, "stage1")
        self.assertEqual(report.work_order, [])

    def test_m50_manifold_produces_vanos_and_ignition_work(self):
        report = evaluate(base_profile(manifold="m50"), {})
        keys = [entry["key"] for entry in report.work_order]
        self.assertIn("vanos_switch_manual", keys)
        self.assertIn("ignition_knock_review", keys)
        self.assertNotIn("vanos_switch_automatic", keys)

    def test_automatic_transmission_uses_its_own_vanos_table(self):
        report = evaluate(base_profile(manifold="m50", transmission="automatic"), {})
        keys = [entry["key"] for entry in report.work_order]
        self.assertIn("vanos_switch_automatic", keys)
        self.assertNotIn("vanos_switch_manual", keys)
        codes = {finding.code for finding in report.findings}
        self.assertIn("VANOS_AUTOMATIC_TABLE", codes)

    def test_work_order_addresses_are_real(self):
        report = evaluate(base_profile(manifold="m50"), {})
        manual = next(e for e in report.work_order if e["key"] == "vanos_switch_manual")
        addresses = {item["symbol"]: item["address"] for item in manual["objects"]}
        self.assertEqual(addresses["ip_maf_min_ivvt__n__tco"], "0x142C")
        self.assertEqual(addresses["id_pvs_ivvt__n"], "0x14F9")

    def test_work_order_carries_the_data_address_for_tables(self):
        report = evaluate(base_profile(popcorn=True, catalyst="removed"), {})
        popcorn = next(e for e in report.work_order if e["key"] == "popcorn_overrun")
        object_map = {item["symbol"]: item for item in popcorn["objects"]}
        self.assertEqual(object_map["ip_n_min_puc__tco"]["data_address"], "0x1D66")
        self.assertEqual(object_map["ip_iga_puc__n"]["data_address"], "0x18E4")
        # Constants keep a single address and must not gain a data address.
        self.assertNotIn("data_address", object_map["c_n_hys_max_puc"])

    def test_stock_ms41_limiter_is_a_fuel_cut_not_ignition(self):
        report = evaluate(base_profile(hardcut=True), {})
        symbols = {
            item["symbol"]
            for entry in report.work_order for item in entry["objects"]
        }
        self.assertIn("c_n_max", symbols)
        self.assertTrue(any("fuel cut" in note for note in report.notes))

    def test_popcorn_with_catalyst_is_rejected(self):
        report = evaluate(base_profile(popcorn=True, catalyst="present"), {})
        self.assertEqual(report.status, "REJECTED")
        codes = {finding.code for finding in report.findings}
        self.assertIn("POPCORN_WITH_CATALYST", codes)

    def test_popcorn_without_catalyst_still_requires_validation(self):
        report = evaluate(
            base_profile(popcorn=True, catalyst="removed",
                         exhaust="headers_and_cat_back"), {}
        )
        codes = {finding.code for finding in report.findings}
        self.assertIn("POPCORN_VALIDATION_REQUIRED", codes)
        self.assertNotIn("POPCORN_WITH_CATALYST", codes)

    def test_foreign_engine_is_rejected(self):
        report = evaluate(base_profile(engine="m52tub28"), {})
        self.assertEqual(report.status, "REJECTED")
        codes = {finding.code for finding in report.findings}
        self.assertIn("ENGINE_NOT_MS41", codes)

    def test_unknown_maf_and_unknown_injectors_cannot_be_combined(self):
        report = evaluate(
            base_profile(
                maf="known_aftermarket", maf_curve_documented=True,
                injectors="known_replacement", injector_flow_cc_min=250.0,
                injector_stock_flow_cc_min=190.0, injector_dead_time_documented=True,
            ),
            {},
        )
        codes = {finding.code for finding in report.findings}
        self.assertIn("MAF_AND_INJECTORS_UNKNOWN", codes)
        self.assertEqual(report.status, "REJECTED")

    def test_injectors_without_flow_or_dead_time_are_rejected(self):
        report = evaluate(base_profile(injectors="known_replacement"), {})
        codes = {finding.code for finding in report.findings}
        self.assertIn("INJECTOR_FLOW_MISSING", codes)
        self.assertIn("INJECTOR_DEAD_TIME_MISSING", codes)

    def test_maf_without_documented_curve_is_rejected(self):
        report = evaluate(
            base_profile(maf="known_aftermarket", maf_curve_documented=False), {}
        )
        codes = {finding.code for finding in report.findings}
        self.assertIn("MAF_CURVE_UNDOCUMENTED", codes)

    def test_ron91_with_increased_compression_is_rejected(self):
        report = evaluate(base_profile(fuel_ron=91, compression="increased_known"), {})
        codes = {finding.code for finding in report.findings}
        self.assertIn("RON91_HIGH_COMPRESSION", codes)

    def test_missing_transmission_blocks_the_preset_key(self):
        profile = base_profile()
        profile.transmission = None
        report = evaluate(profile, {})
        self.assertIsNone(report.preset_key)
        self.assertEqual(report.status, "REJECTED")

    def test_empty_profile_never_yields_a_key(self):
        report = evaluate(HardwareProfile(), {})
        self.assertIsNone(report.preset_key)
        self.assertEqual(report.status, "REJECTED")

    def test_all_engines_are_offered_for_ms41(self):
        self.assertEqual(
            sorted(ENGINES),
            ["m52b20", "m52b25", "m52b28", "s52b32"],
        )


class HeadroomTests(unittest.TestCase):
    def test_duty_cycle_computed_from_ipw_and_rpm(self):
        report = evaluate(base_profile(), {
            "max_injector_pulse_width_ms": 10.0,
            "rpm_at_max_ipw": 6000,
            "max_maf_voltage": 4.0,
        })
        duty = next(f for f in report.findings if f.code == "INJECTOR_DUTY_OK")
        self.assertIn("50.0%", duty.message)

    def test_saturated_injectors_are_rejected(self):
        report = evaluate(base_profile(), {
            "max_injector_pulse_width_ms": 19.5,
            "rpm_at_max_ipw": 6500,
            "max_maf_voltage": 4.0,
        })
        self.assertEqual(report.status, "REJECTED")
        codes = {finding.code for finding in report.findings}
        self.assertIn("INJECTOR_DUTY_REJECT", codes)

    def test_maf_near_adc_ceiling_is_rejected(self):
        report = evaluate(base_profile(), {
            "max_injector_pulse_width_ms": 10.0,
            "rpm_at_max_ipw": 6000,
            "max_maf_voltage": 4.85,
        })
        codes = {finding.code for finding in report.findings}
        self.assertIn("MAF_VOLTAGE_NEAR_CEILING", codes)

    def test_measurements_are_optional_but_flagged(self):
        report = evaluate(base_profile(), None)
        codes = {finding.code for finding in report.findings}
        self.assertIn("MEASUREMENTS_MISSING", codes)
        self.assertNotEqual(report.status, "REJECTED")

    def test_high_trims_reject(self):
        report = evaluate(base_profile(), {
            "max_injector_pulse_width_ms": 10.0,
            "rpm_at_max_ipw": 6000,
            "max_maf_voltage": 4.0,
            "fuel_trims_percent": {"bank1_ltft": 18.0},
        })
        codes = {finding.code for finding in report.findings}
        self.assertIn("TRIM_BANK1_LTFT_HIGH", codes)

    def test_example_measurements_are_healthy(self):
        measurements = json.loads(
            (EXAMPLES / "stage2_measurements.example.json").read_text()
        )
        report = evaluate(base_profile(), measurements)
        codes = {finding.code for finding in report.findings}
        self.assertIn("INJECTOR_DUTY_OK", codes)
        self.assertIn("MAF_VOLTAGE_OK", codes)
        self.assertNotEqual(report.status, "REJECTED")


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from MS42.tools.calibration import DEFAULT_POLICY, Policy
from MS42.tools.generate_patches import (
    EVIDENCE_REQUIREMENTS,
    IMPLEMENTED_KINDS,
    OIL_PUMP_ACK,
    OIL_PUMP_CEILING_RPM,
    RPM_TABLES,
    STATUS_BLOCKED,
    STATUS_EMPTY,
    STATUS_READY,
    STATUS_REQUIRES_DATA,
    generate,
    run as generate_run,
)
from MS42.tools.xdf import load_definition

ROOT = Path(__file__).parents[2]
FIRMWARE = ROOT / "MS42" / "firmware"
B28_LHD = FIRMWARE / "Siemens_MS42_0110C6_E46_M52TUB28_EU3_LHD.bin"
B20 = FIRMWARE / "Siemens_MS42_0110C6_E36Z3_M52TUB20_EU3.bin"
B25_RHD = FIRMWARE / "Siemens_MS42_0110C6_E46_M52TUB25_EU3_RHD.bin"


def policy():
    return Policy.load(DEFAULT_POLICY)


def gen(request, bin_path=B28_LHD):
    return generate(request, bin_path, policy())


def codes(outcome):
    return [finding["code"] for finding in outcome.findings]


class VmaxTests(unittest.TestCase):
    def test_raise_on_a_limited_variant(self):
        result = gen({"intents": [{"id": "v", "kind": "vmax", "value_kmh": 240}]}, B20)
        self.assertEqual(STATUS_READY, result.outcomes[0].status)
        symbols = {change["symbol"] for change in result.patches}
        self.assertEqual({"c_vs_max_1"}, symbols)

    def test_hard_stage_offset_is_optional(self):
        result = gen({"intents": [{"id": "v", "kind": "vmax", "value_kmh": 240,
                                   "hard_add_kmh": 8.0}]}, B20)
        symbols = {change["symbol"] for change in result.patches}
        self.assertEqual({"c_vs_max_1", "c_vs_max_add"}, symbols)

    def test_missing_value_returns_requirements(self):
        result = gen({"intents": [{"id": "v", "kind": "vmax"}]})
        self.assertEqual(STATUS_REQUIRES_DATA, result.outcomes[0].status)
        self.assertEqual("c_vs_max_1", result.outcomes[0].requirements["soft_symbol"])

    def test_disabling_needs_acknowledge_risk(self):
        result = gen({"intents": [{"id": "v", "kind": "vmax", "disable": True}]})
        self.assertEqual(STATUS_REQUIRES_DATA, result.outcomes[0].status)
        self.assertIn("ACKNOWLEDGE_RISK_REQUIRED", codes(result.outcomes[0]))

    def test_disabling_with_risk_acceptance_writes_the_activation_threshold(self):
        result = gen({"intents": [{"id": "v", "kind": "vmax", "disable": True,
                                   "acknowledge_risk": True}]})
        self.assertEqual(STATUS_READY, result.outcomes[0].status)
        change = result.patches[0]
        self.assertEqual("c_n_min_vs_max", change["symbol"])
        self.assertEqual(8160.0, change["value"])

    def test_top_of_scale_on_a_255_variant_changes_nothing(self):
        result = gen({"intents": [{"id": "v", "kind": "vmax", "value_kmh": 255}]})
        self.assertEqual(STATUS_READY, result.outcomes[0].status)
        self.assertEqual("EMPTY", result.core_plan_status)


class RpmLimitTests(unittest.TestCase):
    def test_transmission_is_required(self):
        result = gen({"intents": [{"id": "r", "kind": "rpm_limit", "soft_rpm": 6600}]})
        self.assertEqual(STATUS_BLOCKED, result.outcomes[0].status)
        self.assertIn("TRANSMISSION_REQUIRED", codes(result.outcomes[0]))

    def test_value_is_required(self):
        result = gen({"intents": [{"id": "r", "kind": "rpm_limit",
                                   "transmission": "manual"}]})
        self.assertEqual(STATUS_REQUIRES_DATA, result.outcomes[0].status)

    def test_raising_above_stock_needs_acknowledge_risk(self):
        result = gen({"intents": [{"id": "r", "kind": "rpm_limit",
                                   "transmission": "manual", "soft_rpm": 6800}]})
        self.assertEqual(STATUS_REQUIRES_DATA, result.outcomes[0].status)
        self.assertIn("ACKNOWLEDGE_RISK_REQUIRED", codes(result.outcomes[0]))
        self.assertEqual("id_n_max_mt__nvs",
                         result.outcomes[0].requirements["soft_symbol"])

    def test_raise_with_risk_writes_the_manual_pair(self):
        result = gen({"intents": [{"id": "r", "kind": "rpm_limit",
                                   "transmission": "manual", "soft_rpm": 6688,
                                   "hard_rpm": 6720, "acknowledge_risk": True}]})
        self.assertEqual(STATUS_READY, result.outcomes[0].status)
        symbols = [change["symbol"] for change in result.patches]
        # Emitted per gear: soft before hard within each index.
        self.assertEqual(sorted(["id_n_max_mt__nvs"] * 3 + ["id_n_max_max_mt__nvs"] * 3),
                         sorted(symbols))
        for soft, hard in zip(symbols[0::2], symbols[1::2]):
            self.assertEqual("id_n_max_mt__nvs", soft)
            self.assertEqual("id_n_max_max_mt__nvs", hard)
        self.assertNotIn("id_n_max_at__nvs", symbols)

    def test_auto_gearbox_writes_the_auto_tables(self):
        result = gen({"intents": [{"id": "r", "kind": "rpm_limit",
                                   "transmission": "auto", "soft_rpm": 6600,
                                   "hard_rpm": 6656, "acknowledge_risk": True}]})
        symbols = {change["symbol"] for change in result.patches}
        self.assertIn("id_n_max_at__nvs", symbols)
        self.assertIn("id_n_max_max_at__nvs", symbols)
        self.assertNotIn("id_n_max_mt__nvs", symbols)

    def test_declining_the_limit_is_allowed_without_risk_acceptance(self):
        result = gen({"intents": [{"id": "r", "kind": "rpm_limit",
                                   "transmission": "manual", "soft_rpm": 6300,
                                   "hard_rpm": 6400}]})
        self.assertEqual(STATUS_READY, result.outcomes[0].status)
        self.assertIn("LIMIT_LOWERED_OR_KEPT", codes(result.outcomes[0]))

    def test_soft_above_hard_is_refused(self):
        result = gen({"intents": [{"id": "r", "kind": "rpm_limit",
                                   "transmission": "manual", "soft_rpm": 6800,
                                   "hard_rpm": 6600, "acknowledge_risk": True}]})
        self.assertEqual(STATUS_BLOCKED, result.outcomes[0].status)
        self.assertIn("SOFT_ABOVE_HARD", codes(result.outcomes[0]))

    def test_per_gear_lists_are_applied_element_wise(self):
        result = gen({"intents": [{"id": "r", "kind": "rpm_limit",
                                   "transmission": "manual",
                                   "soft_rpm": [6600, 6500, 6500],
                                   "acknowledge_risk": True}]})
        self.assertEqual(STATUS_READY, result.outcomes[0].status)
        changes = [c for c in result.patches if c["symbol"] == "id_n_max_mt__nvs"]
        self.assertEqual([6600, 6500, 6500], [c["value"] for c in changes])
        self.assertEqual([0, 1, 2], [c["index"] for c in changes])

    def test_the_other_gearbox_tables_are_reported_untouched(self):
        result = gen({"intents": [{"id": "r", "kind": "rpm_limit",
                                   "transmission": "manual", "soft_rpm": 6688,
                                   "acknowledge_risk": True}]})
        self.assertIn("OTHER_GEARBOX_TABLES_UNTOUCHED", codes(result.outcomes[0]))

    def test_hysteresis_objects_are_writable_when_asked_for(self):
        result = gen({"intents": [{"id": "r", "kind": "rpm_limit",
                                   "transmission": "manual", "soft_rpm": 6688,
                                   "hysteresis_rpm": 64, "hysteresis_max_rpm": 352,
                                   "acknowledge_risk": True}]})
        symbols = {change["symbol"] for change in result.patches}
        self.assertTrue({"c_n_max_hys", "c_n_max_hys_max"} <= symbols)

    def test_raising_past_the_oil_pump_ceiling_needs_its_own_acknowledgement(self):
        self.assertEqual(6750.0, OIL_PUMP_CEILING_RPM)
        result = gen({"intents": [{"id": "r", "kind": "rpm_limit",
                                   "transmission": "manual", "soft_rpm": 6900,
                                   "acknowledge_risk": True}]})
        self.assertEqual(STATUS_REQUIRES_DATA, result.outcomes[0].status)
        self.assertIn("HIGH_RPM_OIL_PUMP_ACK_REQUIRED", codes(result.outcomes[0]))
        self.assertEqual(6750.0, result.outcomes[0].requirements["oil_pump_ceiling_rpm"])
        self.assertEqual(OIL_PUMP_ACK, result.outcomes[0].requirements["field"])

    def test_the_ceiling_applies_to_the_hard_limit_too(self):
        result = gen({"intents": [{"id": "r", "kind": "rpm_limit",
                                   "transmission": "manual", "soft_rpm": 6600,
                                   "hard_rpm": 7000, "acknowledge_risk": True}]})
        self.assertEqual(STATUS_REQUIRES_DATA, result.outcomes[0].status)
        self.assertIn("HIGH_RPM_OIL_PUMP_ACK_REQUIRED", codes(result.outcomes[0]))

    def test_the_oil_pump_acknowledgement_lets_the_raise_through(self):
        result = gen({"intents": [{"id": "r", "kind": "rpm_limit",
                                   "transmission": "manual",
                                   "soft_rpm": [6900, 6900, 6900], "hard_rpm": 7000,
                                   "acknowledge_risk": True, OIL_PUMP_ACK: True}]})
        self.assertEqual(STATUS_READY, result.outcomes[0].status)
        values = [change["value"] for change in result.patches
                  if change["symbol"] == "id_n_max_mt__nvs"]
        self.assertEqual([6900, 6900, 6900], values)

    def test_the_ceiling_does_not_apply_at_or_below_it(self):
        result = gen({"intents": [{"id": "r", "kind": "rpm_limit",
                                   "transmission": "auto", "soft_rpm": 6750,
                                   "acknowledge_risk": True}]})
        self.assertEqual(STATUS_READY, result.outcomes[0].status)
        self.assertNotIn("HIGH_RPM_OIL_PUMP_ACK_REQUIRED", codes(result.outcomes[0]))

    def test_the_risk_acknowledgement_still_comes_first(self):
        # Without acknowledge_risk the request stops earlier; the oil pump gate
        # must not be reachable as a shortcut around it.
        result = gen({"intents": [{"id": "r", "kind": "rpm_limit",
                                   "transmission": "manual", "soft_rpm": 6900,
                                   OIL_PUMP_ACK: True}]})
        self.assertEqual(STATUS_REQUIRES_DATA, result.outcomes[0].status)
        self.assertIn("ACKNOWLEDGE_RISK_REQUIRED", codes(result.outcomes[0]))

    def test_tables_are_the_documented_ones(self):
        self.assertEqual("id_n_max_mt__nvs", RPM_TABLES["manual"]["soft"])
        self.assertEqual("id_n_max_max_mt__nvs", RPM_TABLES["manual"]["hard"])
        self.assertEqual("id_n_max_at__nvs", RPM_TABLES["auto"]["soft"])
        self.assertEqual("id_n_max_max_at__nvs", RPM_TABLES["auto"]["hard"])


class HardcutTests(unittest.TestCase):
    def test_missing_values_return_requirements(self):
        result = gen({"intents": [{"id": "h", "kind": "hardcut"}]})
        self.assertEqual(STATUS_REQUIRES_DATA, result.outcomes[0].status)
        self.assertEqual("c_n_hys_max_puc", result.outcomes[0].requirements["upper_symbol"])

    def test_upper_and_lower_are_written(self):
        result = gen({"intents": [{"id": "h", "kind": "hardcut",
                                   "upper_rpm": 704, "lower_rpm": 160}]})
        self.assertEqual(STATUS_READY, result.outcomes[0].status)
        values = {c["symbol"]: c["value"] for c in result.patches}
        self.assertEqual({"c_n_hys_max_puc": 704.0, "c_n_hys_min_puc": 160.0}, values)

    def test_crossed_hysteresis_is_refused(self):
        result = gen({"intents": [{"id": "h", "kind": "hardcut",
                                   "upper_rpm": 200, "lower_rpm": 400}]})
        self.assertEqual(STATUS_BLOCKED, result.outcomes[0].status)
        self.assertIn("HYSTERESIS_INVERTED", codes(result.outcomes[0]))


class EvidenceIntentTests(unittest.TestCase):
    def test_every_evidence_intent_returns_requirements_and_writes_nothing(self):
        for kind in EVIDENCE_REQUIREMENTS:
            with self.subTest(kind=kind):
                result = gen({"intents": [{"id": kind, "kind": kind}]})
                outcome = result.outcomes[0]
                self.assertEqual(STATUS_REQUIRES_DATA, outcome.status)
                self.assertEqual([], outcome.changes)
                self.assertEqual([], result.patches)
                self.assertIn("EVIDENCE_REQUIRED", codes(outcome))

    def test_every_named_symbol_exists_in_the_xdf(self):
        """The MS41 generator once named a symbol that did not exist; never again."""
        objects, problems = load_definition()
        self.assertEqual([], problems)
        for kind, requirement in EVIDENCE_REQUIREMENTS.items():
            for table in requirement["tables"]:
                with self.subTest(kind=kind, symbol=table["symbol"]):
                    self.assertIn(table["symbol"], objects)
            for key in ("soft_symbol", "hard_symbol", "upper_symbol", "lower_symbol",
                        "symbol"):
                if key in requirement:
                    with self.subTest(kind=kind, symbol=requirement[key]):
                        self.assertIn(requirement[key], objects)

    def test_unknown_intent_kind_is_blocked(self):
        result = gen({"intents": [{"id": "x", "kind": "nitrous"}]})
        self.assertEqual(STATUS_BLOCKED, result.outcomes[0].status)
        self.assertIn("INTENT_UNKNOWN", codes(result.outcomes[0]))

    def test_empty_request_is_refused(self):
        from MS42.tools.calibration import PatchError
        with self.assertRaises(PatchError):
            gen({"intents": []})


class GenerationIntegrationTests(unittest.TestCase):
    def test_ready_generation_is_accepted_by_the_core_as_written(self):
        result = gen({
            "intents": [
                {"id": "v", "kind": "vmax", "value_kmh": 250, "hard_add_kmh": 6.0},
                {"id": "r", "kind": "rpm_limit", "transmission": "manual",
                 "soft_rpm": 6688, "hard_rpm": 6720, "acknowledge_risk": True},
            ],
        })
        self.assertEqual(STATUS_READY, result.status)
        self.assertEqual("READY", result.core_plan_status)
        # All eight changes fit in the policy and are inside CRC coverage.
        self.assertEqual(8, len(result.patches))

    def test_generated_file_is_consumable_by_the_calibration_cli(self):
        from MS42.tools import calibration as calibration_module

        request = {"intents": [{"id": "v", "kind": "vmax", "value_kmh": 250}]}
        with tempfile.TemporaryDirectory() as directory:
            request_path = Path(directory) / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            patch_path = Path(directory) / "patches.json"
            namespace = calibration_module.argparse.Namespace(
                bin=B20, request=request_path, out=patch_path, report=None,
                policy=DEFAULT_POLICY, xdf=calibration_module.DEFAULT_XDF, json=True,
            )
            # generate_patches has its own argument surface; call run with it.
            from MS42.tools import generate_patches as generator_module
            self.assertEqual(0, generator_module.run(namespace))
            payload = json.loads(patch_path.read_text(encoding="utf-8"))
            self.assertEqual(STATUS_READY, payload["status"])

            out = Path(directory) / "out.bin"
            calibration_module.run(calibration_module.argparse.Namespace(
                bin=B20, patches=patch_path, out=out, report=None,
                policy=DEFAULT_POLICY, xdf=calibration_module.DEFAULT_XDF, json=False,
            ))
            from MS42.tools.analyze_firmware import analyze
            self.assertTrue(analyze(out).supported)
            self.assertEqual(250, out.read_bytes()[0x4842F])

    def test_implemented_kinds_are_the_documented_ones(self):
        self.assertEqual(("vmax", "rpm_limit", "hardcut"), IMPLEMENTED_KINDS)


if __name__ == "__main__":
    unittest.main()

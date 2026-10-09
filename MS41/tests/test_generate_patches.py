import argparse
import json
import tempfile
import unittest
from pathlib import Path

from MS41.tools import calibration as calibration_module
from MS41.tools.calibration import (
    DEFAULT_POLICY,
    Policy,
    apply_plan,
    load_calibration,
    plan_patches,
)
from MS41.tools.generate_patches import (
    STATUS_BLOCKED,
    STATUS_EMPTY,
    STATUS_READY,
    STATUS_REQUIRES_DATA,
    generate,
    load_request,
    patches_payload,
    stock_values,
)
from MS41.tools.maf_curve import MAF_TITLE
from MS41.tools.xdf import get_object, load_definition, load_objects, read_engineering

ROOT = Path(__file__).parents[2]
FIRMWARE = ROOT / "MS41" / "firmware"
PARTIAL_B28 = FIRMWARE / "MS41_1429861_E36_328i_M52B28_1997_4101_partial.bin"
PARTIAL_B20 = FIRMWARE / "MS41_1429861_E36_320i_M52B20_1996_4101_partial.bin"
EXAMPLE_REQUEST = ROOT / "MS41" / "examples" / "stage1_request.example.json"
EXAMPLE_PROFILE = ROOT / "MS41" / "examples" / "stage2_profile.example.json"


def request(*intents, **extra):
    body = {"version": 1, "customer": "test", "stage": 1, "ron": 95}
    body.update(extra)
    body["intents"] = list(intents)
    return body


def outcome_of(result, ident):
    return next((o for o in result.outcomes if o.id == ident), None)


def profile(**overrides):
    data = json.loads(EXAMPLE_PROFILE.read_text(encoding="utf-8"))
    data.update(overrides)
    return data


class VmaxTests(unittest.TestCase):
    def test_raise_plans_the_speed_limiter(self):
        result = generate(PARTIAL_B28, request(
            {"id": "v", "kind": "vmax", "mode": "raise", "value_kmh": 250}))
        self.assertEqual(result.status, STATUS_READY)
        self.assertEqual(len(result.patches), 1)
        patch = result.patches[0]
        self.assertEqual(patch["symbol"], "c_vs_max")
        self.assertEqual(patch["value"], 250.0)
        self.assertEqual(patch["evidence"], "customer_intent")
        self.assertTrue(patch["requires_validation"])
        self.assertEqual(patch["planned"]["old_engineering"], 255.0)
        self.assertEqual(patch["planned"]["new_engineering"], 250.0)

    def test_remove_sets_the_stock_ceiling(self):
        result = generate(PARTIAL_B28, request(
            {"id": "v", "kind": "vmax", "mode": "remove"}))
        self.assertEqual(result.patches[0]["value"], 255.0)
        self.assertIn("LEGAL_NOTE",
                      {f["code"] for f in outcome_of(result, "v").findings})

    def test_missing_value_is_blocked(self):
        result = generate(PARTIAL_B28, request({"id": "v", "kind": "vmax", "mode": "raise"}))
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn("value_kmh", outcome_of(result, "v").detail)

    def test_value_the_cell_cannot_hold_is_blocked_by_the_planner(self):
        # c_vs_max is an 8-bit cell whose policy ceiling equals the representable
        # maximum, so anything higher fails as unrepresentable.
        result = generate(PARTIAL_B28, request(
            {"id": "v", "kind": "vmax", "mode": "raise", "value_kmh": 300}))
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertEqual(result.patches, [])
        self.assertTrue(result.blocked_reasons)
        self.assertIn("VALUE_NOT_REPRESENTABLE", result.blocked_reasons[0])

    def test_unknown_mode_is_blocked(self):
        result = generate(PARTIAL_B28, request(
            {"id": "v", "kind": "vmax", "mode": "boost", "value_kmh": 200}))
        self.assertEqual(result.status, STATUS_BLOCKED)


class RpmLimitTests(unittest.TestCase):
    def test_raise_without_acknowledgement_waits_for_data(self):
        result = generate(PARTIAL_B28, request(
            {"id": "r", "kind": "rpm_limit", "mode": "raise", "value_rpm": 6700}))
        # Nothing is planned and nothing is refused: the request is waiting on data.
        self.assertEqual(result.status, STATUS_EMPTY)
        self.assertEqual(result.patches, [])
        outcome = outcome_of(result, "r")
        self.assertEqual(outcome.status, STATUS_REQUIRES_DATA)
        self.assertIn("acknowledge_risk", outcome.detail)
        payload = patches_payload(result, request(), None)
        self.assertEqual(payload["unfulfilled"][0]["id"], "r")

    def test_raise_with_acknowledgement_is_planned_with_a_warning(self):
        result = generate(PARTIAL_B28, request(
            {"id": "r", "kind": "rpm_limit", "mode": "raise", "value_rpm": 6700,
             "acknowledge_risk": True}))
        self.assertEqual(result.status, STATUS_READY)
        self.assertEqual(result.patches[0]["symbol"], "c_n_max")
        self.assertIn("RISK_ACKNOWLEDGED",
                      {f["code"] for f in outcome_of(result, "r").findings})

    def test_lowering_the_limiter_needs_no_acknowledgement(self):
        result = generate(PARTIAL_B28, request(
            {"id": "r", "kind": "rpm_limit", "mode": "raise", "value_rpm": 6300}))
        self.assertEqual(result.status, STATUS_READY)
        # 6300 rpm is not representable at a 32 rpm quantum; 6304 is 0.06% away.
        planned = result.patches[0]["planned"]
        self.assertEqual(planned["new_engineering"], 6304.0)
        self.assertAlmostEqual(planned["quantization_error_percent"], 0.0635, places=3)

    def test_value_beyond_the_policy_is_blocked(self):
        result = generate(PARTIAL_B28, request(
            {"id": "r", "kind": "rpm_limit", "mode": "raise", "value_rpm": 6912,
             "acknowledge_risk": True}))
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertEqual(result.patches, [])
        self.assertTrue(result.blocked_reasons)


class HardcutTests(unittest.TestCase):
    def test_explicit_hysteresis_is_planned(self):
        result = generate(PARTIAL_B28, request(
            {"id": "h", "kind": "hardcut", "mode": "harden", "hysteresis_rpm": 96}))
        self.assertEqual(result.patches[0]["symbol"], "c_n_max_hys")
        self.assertEqual(result.patches[0]["planned"]["new_engineering"], 96.0)
        self.assertIn("FUEL_CUT_NOTE",
                      {f["code"] for f in outcome_of(result, "h").findings})

    def test_hysteresis_derived_from_stock_when_omitted(self):
        # Stock is 32 rpm; "half of stock" is not representable, one raw step is.
        result = generate(PARTIAL_B28, request({"id": "h", "kind": "hardcut"}))
        self.assertEqual(result.status, STATUS_READY)
        self.assertEqual(result.patches[0]["planned"]["old_engineering"], 32.0)
        self.assertEqual(result.patches[0]["planned"]["new_engineering"], 0.0)
        self.assertIn("DERIVED_FROM_STOCK",
                      {f["code"] for f in outcome_of(result, "h").findings})

    def test_derived_hysteresis_from_zero_stock_asks_for_a_log(self):
        # M52B20 stock is already the hardest setting; there is nothing to derive.
        result = generate(PARTIAL_B20, request({"id": "h", "kind": "hardcut"}))
        self.assertEqual(result.status, STATUS_EMPTY)
        outcome = outcome_of(result, "h")
        self.assertEqual(outcome.status, STATUS_REQUIRES_DATA)
        self.assertIn("log", outcome.detail)

    def test_soften_doubles_the_stock_value(self):
        result = generate(PARTIAL_B28, request({"id": "h", "kind": "hardcut",
                                                "mode": "soften"}))
        self.assertEqual(result.patches[0]["planned"]["new_engineering"], 64.0)


class UnsupportedIntentTests(unittest.TestCase):
    def test_popcorn_without_catalyst_asks_for_evidence(self):
        result = generate(PARTIAL_B28,
                          request({"id": "p", "kind": "popcorn", "enabled": True},
                                  catalyst="removed"))
        outcome = outcome_of(result, "p")
        self.assertEqual(outcome.status, STATUS_REQUIRES_DATA)
        codes = [f["code"] for f in outcome.findings]
        self.assertIn("EVIDENCE_REQUIRED", codes)
        self.assertIn("TABLES_RESOLVED", codes)
        self.assertIn("NOTHING_WRITTEN", codes)
        # the request names the tables it would touch, with real addresses
        self.assertIn("ip_iga_puc__n", outcome.detail)
        self.assertIn("0x", outcome.detail)

    def test_popcorn_with_catalyst_is_rejected_outright(self):
        result = generate(PARTIAL_B28,
                          request({"id": "p", "kind": "popcorn", "enabled": True},
                                  catalyst="present"))
        outcome = outcome_of(result, "p")
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertEqual(result.patches, [])

    def test_catalyst_flag_can_come_from_the_profile(self):
        result = generate(PARTIAL_B28,
                          request({"id": "p", "kind": "popcorn", "enabled": True}),
                          profile=profile(catalyst="present"))
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn("catalyst", result.blocked_reasons[0])

    def test_unknown_kind_is_blocked(self):
        result = generate(PARTIAL_B28, request({"id": "x", "kind": "nitrous"}))
        self.assertEqual(result.status, STATUS_BLOCKED)
        outcome = outcome_of(result, "x")
        self.assertIn("unknown kind", outcome.detail)
        self.assertIn("known", outcome.detail)


class MafCurveIntentTests(unittest.TestCase):
    STOCK_CURVE = ROOT / "MS41" / "examples" / "maf_curve_stock_b28.example.json"
    EXAMPLE_CURVE = ROOT / "MS41" / "examples" / "maf_curve_example.json"

    def maf_request(self, curve_file=None, **extra):
        intent = {"id": "maf", "kind": "maf_curve"}
        if curve_file is not None:
            intent["curve_file"] = str(curve_file)
        intent.update(extra)
        return request(intent)

    def test_curve_intent_plans_every_changed_column(self):
        result = generate(PARTIAL_B28, self.maf_request(self.EXAMPLE_CURVE),
                          base_dir=ROOT)
        self.assertEqual(result.status, STATUS_READY)
        self.assertEqual(len(result.patches), 154)
        indexes = [p["index"] for p in result.patches]
        self.assertEqual(indexes, sorted(indexes))
        self.assertTrue(all(p["evidence"] == "documented_curve:derived"
                            for p in result.patches))
        codes = {f["code"] for f in outcome_of(result, "maf").findings}
        self.assertIn("CURVE_RESAMPLED", codes)
        self.assertIn("CEILING_ABOVE_STOCK", codes)

    def test_relative_curve_path_is_resolved_against_the_request_directory(self):
        result = generate(PARTIAL_B28,
                          self.maf_request("delivery_note.example.md"), base_dir=ROOT)
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn("does not exist", outcome_of(result, "maf").detail)

    def test_curve_file_is_required(self):
        result = generate(PARTIAL_B28, self.maf_request())
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn("curve_file", outcome_of(result, "maf").detail)

    def test_curve_without_provenance_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "curve.json"
            path.write_text(json.dumps({"points": [[0.0, 0.0], [2.5, 100.0],
                                                   [5.0, 300.0]]}), encoding="utf-8")
            result = generate(PARTIAL_B28, self.maf_request(path))
        self.assertEqual(result.status, STATUS_BLOCKED)
        outcome = outcome_of(result, "maf")
        self.assertIn("PROVENANCE_MISSING", outcome.detail)
        self.assertIn("CURVE_REFUSED", {f["code"] for f in outcome.findings})

    def test_curve_that_matches_the_binary_is_empty(self):
        result = generate(PARTIAL_B28, self.maf_request(self.STOCK_CURVE), base_dir=ROOT)
        self.assertEqual(result.status, STATUS_EMPTY)
        self.assertEqual(result.patches, [])
        self.assertIn("already matches", outcome_of(result, "maf").detail)

    def test_curve_plan_is_accepted_by_the_core(self):
        req = self.maf_request(self.EXAMPLE_CURVE)
        result = generate(PARTIAL_B28, req, base_dir=ROOT)
        payload = patches_payload(result, req, None)
        objects, problems = load_definition()
        image, offset, image_type = calibration_module.load_calibration(PARTIAL_B28)
        plan = plan_patches(image, offset, image_type, objects, problems,
                            payload["patches"], Policy.load(DEFAULT_POLICY))
        self.assertEqual(plan.status, "READY")
        self.assertEqual(len(plan.changes), 154)
        self.assertEqual({c.symbol for c in plan.changes}, {MAF_TITLE})


class ProfileGateTests(unittest.TestCase):
    def test_rejected_profile_blocks_the_whole_request(self):
        result = generate(PARTIAL_B28,
                          request({"id": "v", "kind": "vmax", "mode": "raise",
                                   "value_kmh": 250}),
                          profile=profile(engine="m52tu"))
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertEqual(result.patches, [])
        self.assertIn("HARDWARE_PROFILE_REJECTED", result.blocked_reasons[0])

    def test_incomplete_profile_blocks_the_whole_request(self):
        result = generate(PARTIAL_B28, request({"id": "v", "kind": "vmax",
                                                "mode": "raise", "value_kmh": 250}),
                          profile={"engine": "m52b28"})
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertTrue(any("DME_VARIANT_MISSING" in reason
                            for reason in result.blocked_reasons))

    def test_accepted_profile_adds_work_order_notes(self):
        result = generate(PARTIAL_B28,
                          request({"id": "v", "kind": "vmax", "mode": "raise",
                                   "value_kmh": 250}),
                          profile=profile())
        self.assertEqual(result.status, STATUS_READY)
        self.assertTrue(any("hardware profile status" in note for note in result.notes))
        self.assertTrue(any("work order" in note or "required logging" in note
                            for note in result.notes))


class LaunchLimiterTests(unittest.TestCase):
    """MS41 limits speed by shutting cylinders off - that is the launch limiter."""

    PROFILE = ROOT / "MS41" / "examples" / "stage2_profile.example.json"

    def profile(self, catalyst="removed"):
        data = json.loads(self.PROFILE.read_text(encoding="utf-8"))
        data["catalyst"] = catalyst
        return data

    def test_a_launch_limit_needs_the_risk_acknowledgement(self):
        result = generate(PARTIAL_B28,
                          request({"id": "l", "kind": "launch_limiter", "value_kmh": 40}),
                          profile=self.profile())
        outcome = outcome_of(result, "l")
        self.assertEqual(outcome.status, STATUS_REQUIRES_DATA)
        self.assertIn("cannot exceed", outcome.detail)
        self.assertEqual(result.patches, [])

    def test_the_acknowledgement_enables_the_variant_and_sets_the_limit(self):
        result = generate(PARTIAL_B28,
                          request({"id": "l", "kind": "launch_limiter", "value_kmh": 40,
                                   "acknowledge_risk": True}),
                          profile=self.profile())
        outcome = outcome_of(result, "l")
        self.assertEqual(outcome.status, STATUS_READY, outcome.detail)
        self.assertEqual([c["symbol"] for c in outcome.changes],
                         ["c_var_vs_max_act", "c_vs_max"])
        self.assertEqual(outcome.changes[0]["value"], 1)
        self.assertEqual(outcome.changes[1]["value"], 40.0)
        codes = [f["code"] for f in outcome.findings]
        self.assertIn("LAUNCH_LIMIT_MECHANISM", codes)
        self.assertIn("ESCALATION_KEPT", codes)

    def test_a_catalyst_refuses_the_launch_limit(self):
        result = generate(PARTIAL_B28,
                          request({"id": "l", "kind": "launch_limiter", "value_kmh": 40,
                                   "acknowledge_risk": True}),
                          profile=self.profile(catalyst="present"))
        outcome = outcome_of(result, "l")
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn("LAUNCH_WITH_CATALYST", outcome.detail)
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertEqual(result.patches, [])

    def test_a_road_speed_limit_is_allowed_on_a_catalyst_car(self):
        result = generate(PARTIAL_B28,
                          request({"id": "l", "kind": "launch_limiter", "value_kmh": 180,
                                   "acknowledge_risk": True}),
                          profile=self.profile(catalyst="present"))
        outcome = outcome_of(result, "l")
        self.assertEqual(outcome.status, STATUS_READY, outcome.detail)
        self.assertIn("ROAD_LIMIT", [f["code"] for f in outcome.findings])

    def test_the_escalation_table_stays_stock(self):
        result = generate(PARTIAL_B28,
                          request({"id": "l", "kind": "launch_limiter", "value_kmh": 40,
                                   "acknowledge_risk": True}),
                          profile=self.profile())
        outcome = outcome_of(result, "l")
        self.assertNotIn("id_n_max_cycnr__vs", {c["symbol"] for c in outcome.changes})
        finding = next(f for f in outcome.findings if f["code"] == "ESCALATION_KEPT")
        self.assertIn("0x1974", finding["message"])

    def test_the_limit_must_be_representable(self):
        for value in (0, 400):
            result = generate(PARTIAL_B28,
                              request({"id": "l", "kind": "launch_limiter",
                                       "value_kmh": value, "acknowledge_risk": True}),
                              profile=self.profile())
            self.assertEqual(outcome_of(result, "l").status, STATUS_BLOCKED, value)

    def test_the_escalation_is_reported_from_the_file(self):
        # the same symbol carries different ladders per engine family, so the
        # report has to come from the image being patched, not from a constant
        case = request({"id": "l", "kind": "launch_limiter", "value_kmh": 40,
                        "acknowledge_risk": True})
        expected = {
            PARTIAL_B28: "38 км/ч->13, 90 км/ч->19, 142 км/ч->23, 196 км/ч->26, 234 км/ч->26",
            PARTIAL_B20: "30 км/ч->50, 70 км/ч->45, 110 км/ч->40, 160 км/ч->30, 200 км/ч->13",
        }
        for path, line in expected.items():
            result = generate(path, case, profile=self.profile())
            finding = next(f for f in outcome_of(result, "l").findings
                           if f["code"] == "ESCALATION_KEPT")
            self.assertIn(line, finding["message"], path.name)

    def test_the_planned_change_passes_the_real_patcher(self):
        from MS41.tools.xdf import load_definition
        result = generate(PARTIAL_B28,
                          request({"id": "l", "kind": "launch_limiter", "value_kmh": 40,
                                   "acknowledge_risk": True}),
                          profile=self.profile())
        payload = patches_payload(result, request({"id": "l", "kind": "launch_limiter",
                                                   "value_kmh": 40}), self.profile())
        objects, problems = load_definition()
        image, offset, image_type = load_calibration(PARTIAL_B28)
        plan = plan_patches(image, offset, image_type, objects, problems,
                            payload["patches"], Policy.load(DEFAULT_POLICY))
        self.assertEqual(plan.status, STATUS_READY, [f.message for f in plan.findings])
        patched, blocks = apply_plan(image, offset, plan, objects)
        self.assertEqual(blocks, 15)
        self.assertEqual(patched[offset + 0x271], 1)
        self.assertEqual(patched[offset + 0x272], 40)


class EvidenceIntentTests(unittest.TestCase):
    """Kinds that need a bench answer come back with a plan, not a refusal."""

    def test_every_requirement_names_tables_that_exist(self):
        from MS41.tools.generate_patches import EVIDENCE_REQUIREMENTS
        from MS41.tools.xdf import load_objects
        objects = load_objects()
        self.assertTrue(EVIDENCE_REQUIREMENTS)
        for kind, requirement in EVIDENCE_REQUIREMENTS.items():
            for symbol in requirement["tables"]:
                self.assertIn(symbol, objects, f"{kind}: {symbol} is not in the XDF")

    def test_the_plan_resolves_real_addresses(self):
        for kind, symbol in (("ignition", "ip_igab__n__maf"),
                             ("fueling", "ip_ti_fl__n"),
                             ("popcorn", "ip_iga_puc__n")):
            result = generate(PARTIAL_B28, request({"id": kind, "kind": kind}))
            outcome = outcome_of(result, kind)
            self.assertEqual(outcome.status, STATUS_REQUIRES_DATA, kind)
            self.assertIn(symbol, outcome.detail)
            self.assertIn("0x", outcome.detail)
            self.assertEqual(outcome.changes, [])
            self.assertTrue(all(finding["severity"] != "reject"
                                for finding in outcome.findings), kind)

    def test_a_requirement_only_request_writes_nothing(self):
        result = generate(PARTIAL_B28, request({"id": "i", "kind": "ignition"},
                                               {"id": "f", "kind": "fueling"}))
        self.assertEqual(result.patches, [])
        self.assertEqual(result.status, STATUS_EMPTY)

    def test_unknown_kinds_list_the_known_ones(self):
        result = generate(PARTIAL_B28, request({"id": "x", "kind": "nitrous"}))
        outcome = outcome_of(result, "x")
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn("launch_limiter", outcome.detail)
        self.assertIn("ignition", outcome.detail)
class Variant1Tests(unittest.TestCase):
    """Октан и опции варианта 1 на MS41 (шаг 1.4 задания).

    У MS41 нет пары карт RON91/RON98: база считается одной картой с октановым
    фактором, семантику которого на стоке мы не доказывали, — значит профиль
    честно ждёт данных. DISA и вентилятор/термостат на MS41 отсутствуют как
    объекты, поэтому это отказ с причиной, а не «ждём данных».
    """

    def test_octane_waits_for_the_factor_semantics(self):
        result = generate(PARTIAL_B28, request({"id": "o", "kind": "octane",
                                                "fuel": "ron98"}))
        outcome = outcome_of(result, "o")
        self.assertEqual(outcome.status, STATUS_REQUIRES_DATA)
        self.assertIn("ip_iga_tra_fac__tia__ron_fac", outcome.detail)
        self.assertEqual(outcome.changes, [])
        self.assertEqual(result.patches, [])
        self.assertTrue(all(finding["severity"] != "reject"
                            for finding in outcome.findings))

    def test_the_octane_requirement_is_gated_on_the_knock_protections(self):
        from MS41.tools.generate_patches import EVIDENCE_REQUIREMENTS
        requirement = EVIDENCE_REQUIREMENTS["octane"]
        gates = " ".join(requirement["gates"])
        self.assertIn("ip_iga_tra_fac__tia__ron_fac", gates)
        self.assertIn("защит", gates)

    def test_unavailable_options_are_refused_with_a_reason(self):
        for kind, needle in (("disa_off", "DISA"),
                             ("fan_thermostat", "вентилятор"),
                             ("launch", "launch control")):
            with self.subTest(kind=kind):
                result = generate(PARTIAL_B28, request({"id": kind, "kind": kind}))
                outcome = outcome_of(result, kind)
                self.assertEqual(outcome.status, STATUS_BLOCKED, kind)
                self.assertIn(needle, outcome.detail)
                self.assertEqual(result.patches, [])
                self.assertEqual(result.status, STATUS_BLOCKED)

    def test_isa_off_and_idle_wait_for_an_idle_log(self):
        for kind in ("isa_off", "idle"):
            with self.subTest(kind=kind):
                result = generate(PARTIAL_B28, request({"id": kind, "kind": kind}))
                outcome = outcome_of(result, kind)
                self.assertEqual(outcome.status, STATUS_REQUIRES_DATA, kind)
                self.assertIn("0x", outcome.detail)
                self.assertEqual(outcome.changes, [])


class PatchFileTests(unittest.TestCase):
    def test_payload_status_and_patch_list_shape(self):
        req = request({"id": "v", "kind": "vmax", "mode": "raise", "value_kmh": 250})
        result = generate(PARTIAL_B28, req)
        payload = patches_payload(result, req, None)
        self.assertEqual(payload["status"], "READY")
        self.assertEqual(payload["generated_from_sha256"], result.source_sha256)
        self.assertEqual(payload["patches"][0]["symbol"], "c_vs_max")
        self.assertEqual(payload["planned_details"]["c_vs_max[0]"]["address"], "0x0272")
        self.assertNotIn("planned", payload["patches"][0])

    def test_example_request_file_documents_the_blocked_workflow(self):
        req = load_request(EXAMPLE_REQUEST)
        result = generate(PARTIAL_B28, req)
        self.assertEqual(result.status, STATUS_BLOCKED, "popcorn + catalyst is a block")
        self.assertEqual(len(result.patches), 2, "vmax and hardcut still pass")
        self.assertEqual(outcome_of(result, "rpm").status, STATUS_REQUIRES_DATA)

    def test_generated_patches_are_accepted_by_the_core(self):
        req = request(
            {"id": "v", "kind": "vmax", "mode": "raise", "value_kmh": 250},
            {"id": "h", "kind": "hardcut", "mode": "harden", "hysteresis_rpm": 96},
        )
        result = generate(PARTIAL_B28, req)
        payload = patches_payload(result, req, None)
        objects, problems = load_definition()
        image, offset, image_type = calibration_module.load_calibration(PARTIAL_B28)
        plan = plan_patches(image, offset, image_type, objects, problems,
                            payload["patches"], Policy.load(DEFAULT_POLICY))
        self.assertEqual(plan.status, "READY")
        self.assertEqual({c.symbol for c in plan.changes}, {"c_vs_max", "c_n_max_hys"})

    def test_generate_then_apply_lands_the_requested_values(self):
        req = request(
            {"id": "v", "kind": "vmax", "mode": "raise", "value_kmh": 250},
            {"id": "h", "kind": "hardcut", "mode": "harden", "hysteresis_rpm": 96},
        )
        result = generate(PARTIAL_B28, req)
        objects, problems = load_definition()
        image, offset, image_type = calibration_module.load_calibration(PARTIAL_B28)
        plan = plan_patches(image, offset, image_type, objects, problems,
                            patches_payload(result, req, None)["patches"],
                            Policy.load(DEFAULT_POLICY))
        output, blocks = apply_plan(image, offset, plan, objects)
        self.assertEqual(blocks, 15)
        calibration = bytes(output[offset:offset + 0x6000])
        self.assertTrue(calibration_module.verify_crc_chain(calibration)[0])
        self.assertAlmostEqual(
            read_engineering(calibration, get_object(objects, "c_vs_max"))[0], 250.0)
        self.assertAlmostEqual(
            read_engineering(calibration, get_object(objects, "c_n_max_hys"))[0], 96.0)

    def test_calibration_cli_refuses_a_blocked_patch_file(self):
        req = request({"id": "v", "kind": "vmax", "mode": "raise", "value_kmh": 250})
        payload = patches_payload(generate(PARTIAL_B28, req), req, None)
        payload["status"] = "BLOCKED"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "patches.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            args = argparse.Namespace(
                bin=PARTIAL_B28, patches=path, out=None, report=None,
                policy=DEFAULT_POLICY, xdf=calibration_module.DEFAULT_XDF, json=True,
            )
            with self.assertRaises(SystemExit) as caught:
                calibration_module.run(args)
        self.assertIn("BLOCKED", str(caught.exception))

    def test_stock_values_helper_reads_the_fixture(self):
        objects = load_objects()
        image, offset, _ = calibration_module.load_calibration(PARTIAL_B28)
        values = stock_values(image, offset, objects)
        self.assertEqual(values["c_n_max"], 6496.0)
        self.assertEqual(values["c_vs_max"], 255.0)
        self.assertEqual(values["c_n_max_hys"], 32.0)


if __name__ == "__main__":
    unittest.main()

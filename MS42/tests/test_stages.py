import json
import tempfile
import unittest
from pathlib import Path

from MS42.tools.knowledge_base import (
    DEFAULT_KNOWLEDGE,
    VERDICTS,
    VERDICT_RU,
    load,
    render_html,
)
from MS42.tools.stages import (
    CONSUMPTION_SYMBOL,
    DEAD_TIME_SYMBOL,
    PACKAGES,
    PULSE_WIDTH_MAPS,
    STATUS_BLOCKED,
    STATUS_EMPTY,
    STATUS_READY,
    STATUS_REQUIRES_DATA,
    build,
    build_payload,
    generation_payload,
    load_injector_profile,
    load_maf_curve,
    packages_for,
)
from MS42.tools.calibration import DEFAULT_POLICY, DEFAULT_XDF, Policy, PatchError
from MS42.tools.donor import DEFAULT_DONOR_BIN, DonorError, load_donor
from MS42.tools.popcorn import INTENSITY_BANDS, load_profile, parse_profile

ROOT = Path(__file__).parents[2]
FIRMWARE = ROOT / "MS42" / "firmware"
B28_LHD = FIRMWARE / "Siemens_MS42_0110C6_E46_M52TUB28_EU3_LHD.bin"
B20 = FIRMWARE / "Siemens_MS42_0110C6_E36Z3_M52TUB20_EU3.bin"
EXAMPLES = ROOT / "MS42" / "examples"


def policy():
    return Policy.load(DEFAULT_POLICY)


def write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def stage1_request(**overrides) -> dict:
    request = {
        "intents": [
            {"id": "v1", "kind": "vmax", "value_kmh": 240, "hard_add_kmh": 8.0},
            {"id": "r1", "kind": "rpm_limit", "transmission": "manual",
             "soft_rpm": 6688, "hard_rpm": 6720, "acknowledge_risk": True},
        ],
    }
    request.update(overrides)
    return request


def injector_profile(**overrides) -> dict:
    payload = {"kind": "injectors", "stock_flow": 190, "new_flow": 240,
               "source": "test datasheet"}
    payload.update(overrides)
    return payload


def maf_curve(values, **overrides) -> dict:
    payload = {"kind": "maf_curve", "values_kg_h": values,
               "provenance": "documented_curve:test bench"}
    payload.update(overrides)
    return payload


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.kb = load(DEFAULT_KNOWLEDGE, xdf=DEFAULT_XDF)

    def test_file_is_valid(self):
        self.assertTrue(self.kb.ok, self.kb.problems)
        self.assertGreaterEqual(len(self.kb.entries), 25)

    def test_every_symbol_exists_in_the_xdf(self):
        # The loader already checks this; assert it is actually checking.
        broken = dict(json.loads(DEFAULT_KNOWLEDGE.read_text(encoding="utf-8")))
        broken["entries"] = [dict(broken["entries"][0], symbols=["not_a_real_symbol"])]
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(Path(directory) / "kb.json", broken)
            kb = load(path, xdf=DEFAULT_XDF)
            self.assertFalse(kb.ok)
            self.assertTrue(any("not_a_real_symbol" in problem for problem in kb.problems))

    def test_broken_schema_and_sources_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kb.json"
            write_json(path, {"schema": 99, "sources": [{"id": "x", "title": "t",
                                                         "url": "file:///etc"}]})
            kb = load(path, xdf=DEFAULT_XDF)
            self.assertFalse(kb.ok)
            self.assertTrue(any("schema" in problem for problem in kb.problems))
            self.assertTrue(any("http" in problem for problem in kb.problems))

    def test_every_entry_names_a_known_source_and_verdict(self):
        for entry in self.kb.entries:
            with self.subTest(entry=entry.id):
                self.assertIn(entry.verdict, VERDICTS)
                self.assertTrue(entry.sources)
                for source_id in entry.sources:
                    self.assertIn(source_id, self.kb.sources)

    def test_the_stage2_recipes_are_gated_not_implemented(self):
        expected = {
            "injector-scaling": "gated",
            "injector-dead-time": "gated",
            "fuel-consumption-gauge": "gated",
            "maf-curve": "gated",
            "ignition-ron": "research",
            "exhaust-pops": "gated",
            "dtc-suppression": "refused",
        }
        for entry_id, verdict in expected.items():
            with self.subTest(entry=entry_id):
                entry = self.kb.entry(entry_id)
                self.assertIsNotNone(entry, entry_id)
                self.assertEqual(verdict, entry.verdict)

    def test_the_implemented_entries_are_the_tools_we_have(self):
        implemented = {entry.id for entry in self.kb.entries
                       if entry.verdict == "implemented"}
        self.assertTrue({"vmax", "rpm-limit", "hardcut", "checksums",
                         "memory-layout"} <= implemented)

    def test_queries(self):
        self.assertTrue(all(1 in entry.stage for entry in self.kb.query(stage=1)))
        self.assertTrue(all(entry.verdict == "refused"
                            for entry in self.kb.query(verdict="refused")))
        symbols = self.kb.for_symbol("ip_iga_ron_98_pl_ivvt__n__maf")
        self.assertEqual({"ignition-ron", "octane-bins"}, {entry.id for entry in symbols})
        found = self.kb.query(text="форсун")
        self.assertTrue(any(entry.id == "injector-scaling" for entry in found))

    def test_html_is_self_contained_and_contains_every_entry(self):
        page = render_html(self.kb)
        self.assertTrue(page.startswith("<!DOCTYPE html>"))
        for entry in self.kb.entries:
            self.assertIn(entry.title.replace("&", "&amp;"), page)
        self.assertIn("id=\"search\"", page)
        self.assertIn("data-verdict=", page)
        # No external scripts or styles: the site must work offline.
        self.assertNotIn("<script src", page)
        self.assertNotIn("<link rel=\"stylesheet\"", page)
        for verdict in VERDICTS:
            self.assertIn(VERDICT_RU[verdict], page)


class StagePackageTests(unittest.TestCase):
    def test_stage1_packages(self):
        ids = [package.id for package in packages_for(1)]
        self.assertEqual(
            ["limits", "overrun_pops", "idle", "ignition", "fueling",
             "octane", "disa_off", "isa_off", "fan_thermostat"], ids)

    def test_stage2_adds_the_hardware_packages(self):
        ids = [package.id for package in packages_for(2)]
        self.assertTrue({"injectors", "maf", "intake", "cams", "exhaust"} <= set(ids))
        self.assertEqual(len(PACKAGES), len(ids))

    def test_every_package_declares_a_source_and_stage(self):
        for package in PACKAGES:
            with self.subTest(package=package.id):
                self.assertIn(package.stage, (1, 2))
                self.assertTrue(package.sources)
                self.assertTrue(package.summary)


class InjectorProfileTests(unittest.TestCase):
    def test_valid_profile_factor_direction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(Path(directory) / "inj.json", injector_profile())
            profile = load_injector_profile(path)
            self.assertAlmostEqual(190 / 240, profile.factor, places=6)
            self.assertLess(profile.factor, 1.0)   # more flow, shorter pulse

    def test_source_is_mandatory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(Path(directory) / "inj.json", injector_profile(source=""))
            with self.assertRaises(PatchError):
                load_injector_profile(path)

    def test_flow_ratio_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(Path(directory) / "inj.json",
                              injector_profile(new_flow=1000))
            with self.assertRaises(PatchError):
                load_injector_profile(path)

    def test_dead_time_needs_eight_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(Path(directory) / "inj.json",
                              injector_profile(dead_time_ms=[1, 2, 3]))
            with self.assertRaises(PatchError):
                load_injector_profile(path)

    def test_kind_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(Path(directory) / "inj.json", injector_profile(kind="maf_curve"))
            with self.assertRaises(PatchError):
                load_injector_profile(path)

    def test_non_positive_flow_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(Path(directory) / "inj.json", injector_profile(new_flow=0))
            with self.assertRaises(PatchError):
                load_injector_profile(path)


class MafCurveTests(unittest.TestCase):
    def curve(self, count=256, scale=1.0):
        values = [round(3.8 * index * scale, 6) for index in range(count)]
        return maf_curve(values)

    def test_valid_curve(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(Path(directory) / "maf.json", self.curve())
            curve = load_maf_curve(path)
            self.assertEqual(256, len(curve.values))
            self.assertTrue(curve.provenance.startswith("documented_curve:"))

    def test_provenance_must_be_documented(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(Path(directory) / "maf.json",
                              self.curve() | {"provenance": "from a forum post"})
            with self.assertRaises(PatchError):
                load_maf_curve(path)

    def test_curve_must_be_non_decreasing(self):
        values = [float(index) for index in range(256)]
        values[10] = 99.0
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(Path(directory) / "maf.json", maf_curve(values))
            with self.assertRaises(PatchError):
                load_maf_curve(path)

    def test_curve_must_have_the_right_length(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(Path(directory) / "maf.json", maf_curve([1.0, 2.0]))
            with self.assertRaises(PatchError):
                load_maf_curve(path)

    def test_curve_values_are_bounded(self):
        values = [float(index) for index in range(256)]
        values[255] = 2048.0
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(Path(directory) / "maf.json", maf_curve(values))
            with self.assertRaises(PatchError):
                load_maf_curve(path)


class StageBuildTests(unittest.TestCase):
    def test_stage1_with_limits_writes_a_verified_file(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "stage1.bin"
            result = build("stage1", B20, stage1_request(), policy(), out=out)
            self.assertEqual(STATUS_READY, result.status)
            self.assertTrue(out.exists())
            self.assertEqual("WRITTEN", result.core_plan_status)
            self.assertTrue(result.crc_valid_after)
            self.assertEqual([], result.unplanned_byte_changes)
            self.assertEqual(STATUS_READY, result.package("limits").status)
            for package_id in ("overrun_pops", "idle", "ignition", "fueling"):
                with self.subTest(package=package_id):
                    self.assertEqual(STATUS_REQUIRES_DATA, result.package(package_id).status)
                    self.assertEqual([], result.package(package_id).changes)

    def test_stage1_without_limits_has_nothing_to_write(self):
        result = build("stage1", B20, {"intents": [{"id": "p", "kind": "popcorn"}]},
                       policy())
        self.assertEqual(STATUS_EMPTY, result.status)
        self.assertEqual("EMPTY", result.core_plan_status)
        self.assertEqual(STATUS_REQUIRES_DATA, result.package("limits").status)
        self.assertEqual(STATUS_REQUIRES_DATA, result.package("overrun_pops").status)

    def test_stage1_keeps_every_evidence_package_out_of_the_plan(self):
        result = build("stage1", B20, stage1_request(), policy())
        written = {change["symbol"] for change in result.patches}
        for symbol in ("ip_iga_ron_98_pl_ivvt__n__maf", "ip_tib__n__maf",
                       "ip_n_min_puc__tco", "ip_n_sp_is__tco"):
            self.assertNotIn(symbol, written)
        self.assertEqual({"c_vs_max_1", "c_vs_max_add", "id_n_max_mt__nvs",
                          "id_n_max_max_mt__nvs"}, written)

    def test_stage2_without_hardware_is_refused(self):
        result = build("stage2", B28_LHD, stage1_request(), policy())
        self.assertEqual(STATUS_BLOCKED, result.status)
        self.assertIn("HARDWARE_REQUIRED", [item["code"] for item in result.findings])
        self.assertEqual([], result.packages)

    def test_stage2_with_hardware_but_no_documents_waits_for_them(self):
        request = stage1_request(hardware={"engine": "M52TUB28", "intake": "M50 manifold"})
        result = build("stage2", B28_LHD, request, policy())
        self.assertEqual(STATUS_READY, result.status)          # limits were written
        self.assertEqual(STATUS_READY, result.package("limits").status)
        self.assertEqual(STATUS_REQUIRES_DATA, result.package("injectors").status)
        self.assertEqual(STATUS_REQUIRES_DATA, result.package("maf").status)
        self.assertEqual([], result.package("injectors").changes)
        self.assertIn("stock_flow", result.package("injectors").requirements["fields"][1])

    def test_stage2_with_documents_writes_the_scaled_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            profile_path = write_json(directory / "inj.json", injector_profile(
                dead_time_ms=[2.4, 1.6, 0.9, 0.65, 0.45, 0.34, 0.26, 0.2]))
            out = directory / "stage2.bin"
            request = stage1_request(hardware={"engine": "M52TUB28",
                                               "injectors": "240 cc/min"})
            result = build("stage2", B28_LHD, request, policy(), out=out,
                           injectors=load_injector_profile(profile_path))
            self.assertEqual(STATUS_READY, result.status)
            self.assertTrue(out.exists())
            self.assertTrue(result.crc_valid_after)
            injectors = result.package("injectors")
            self.assertEqual(STATUS_READY, injectors.status)
            symbols = {change["symbol"] for change in injectors.changes}
            self.assertTrue(set(PULSE_WIDTH_MAPS) <= symbols)
            self.assertIn(DEAD_TIME_SYMBOL, symbols)
            self.assertIn(CONSUMPTION_SYMBOL, symbols)
            self.assertGreater(result.value_bytes_changed, 500)

    def test_injector_math_is_the_documented_one(self):
        from MS42.tools.stages import injector_changes
        from MS42.tools.xdf import get_object, load_definition, read_engineering
        from MS42.tools.calibration import load_calibration

        objects, _ = load_definition(DEFAULT_XDF)
        image, offset, _type, _base = load_calibration(B28_LHD)
        cal = bytes(image[offset:offset + 0x8000])
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(Path(directory) / "inj.json", injector_profile())
            profile = load_injector_profile(path)
        changes = injector_changes(profile, image, offset, objects)
        factor = profile.factor
        pulse = [change for change in changes if change["symbol"] == PULSE_WIDTH_MAPS[0]]
        obj = get_object(objects, PULSE_WIDTH_MAPS[0])
        stock = read_engineering(cal, obj)
        for index, change in enumerate(pulse):
            self.assertAlmostEqual(stock[index] * factor, change["value"], places=6)
        consumption = [change for change in changes if change["symbol"] == CONSUMPTION_SYMBOL]
        obj = get_object(objects, CONSUMPTION_SYMBOL)
        stock = read_engineering(cal, obj)
        for index, change in enumerate(consumption):
            self.assertAlmostEqual(stock[index] / factor, change["value"], places=6)

    def test_documented_maf_curve_is_written(self):
        # A uniform 4 % lift keeps the curve non-decreasing and inside bounds.
        from MS42.tools.xdf import get_object, load_definition, read_engineering
        from MS42.tools.calibration import load_calibration

        objects, _ = load_definition(DEFAULT_XDF)
        image, offset, _type, _base = load_calibration(B28_LHD)
        stock = read_engineering(bytes(image[offset:offset + 0x8000]),
                                 get_object(objects, "id_maf_tab"))
        values = [round(value * 1.04, 6) for value in stock]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            path = write_json(directory / "maf.json", maf_curve(values))
            curve = load_maf_curve(path)
            out = directory / "out.bin"
            result = build("stage2", B28_LHD,
                           stage1_request(hardware={"engine": "M52TUB28", "maf": "test"}),
                           policy(), maf_curve=curve, out=out)
            self.assertEqual(STATUS_READY, result.status)
            self.assertTrue(out.exists())
            self.assertTrue(result.crc_valid_after)
            package = result.package("maf")
            self.assertEqual(STATUS_READY, package.status)
            self.assertEqual(256, len(package.changes))
            self.assertGreater(result.value_bytes_changed, 200)

    def test_a_stage2_file_is_keyed_to_its_dataset(self):
        request = stage1_request(hardware={"engine": "M52TUB28"})
        first = build("stage2", B28_LHD, request, policy())
        second = build("stage2", B20, request, policy())
        self.assertEqual("91c6322g", first.dataset_id)
        self.assertEqual("91c6w22g", second.dataset_id)
        self.assertNotEqual(first.source_sha256, second.source_sha256)
        self.assertEqual(request["hardware"], first.hardware)
        self.assertEqual(policy().version, first.policy_version)

    def test_unknown_stage_is_refused(self):
        with self.assertRaises(PatchError):
            build("stage3", B20, stage1_request(), policy())

    def test_the_output_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            first = build("stage2", B28_LHD, stage1_request(hardware={"engine": "M52TUB28"}),
                          policy(), out=directory / "a.bin")
            second = build("stage2", B28_LHD, stage1_request(hardware={"engine": "M52TUB28"}),
                           policy(), out=directory / "b.bin")
            self.assertEqual(first.output_sha256, second.output_sha256)
            self.assertEqual((directory / "a.bin").read_bytes(),
                             (directory / "b.bin").read_bytes())

    def test_a_rejected_plan_leaves_no_file_behind(self):
        # The B20 image has c_n_min_vs_max = 800; asking for an impossible raw
        # value through the limits intent must not produce a file.
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out.bin"
            request = {"intents": [{"id": "v", "kind": "vmax", "value_kmh": 500}]}
            result = build("stage1", B20, request, policy(), out=out)
            self.assertEqual(STATUS_BLOCKED, result.status)
            self.assertFalse(out.exists())

    def test_payload_shape(self):
        result = build("stage1", B20, stage1_request(), policy())
        payload = build_payload(result)
        for key in ("stage", "status", "dataset_id", "osid", "image_type",
                    "policy_version", "hardware", "donor", "packages", "patches",
                    "crc_valid_after", "unplanned_byte_changes"):
            self.assertIn(key, payload)
        self.assertEqual("stage1", payload["stage"])
        note_input = generation_payload(result)
        self.assertIn("intents", note_input)
        self.assertEqual(len(result.packages), len(note_input["intents"]))

    def test_example_requests_are_valid_inputs(self):
        for name, stage in (("stage1_request.example.json", "stage1"),
                            ("stage2_request.example.json", "stage2")):
            with self.subTest(example=name):
                request = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
                result = build(stage, B28_LHD, request, policy())
                self.assertIn(result.status, (STATUS_READY, STATUS_EMPTY))
                self.assertTrue(result.packages)

    def test_stage1_example_produces_a_note_and_a_file(self):
        request = json.loads((EXAMPLES / "stage1_request.example.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "stage1.bin"
            result = build("stage1", B20, request, policy(), out=out)
            from MS42.tools.delivery_note import build_note
            note = build_note(generation_payload(result), None, "Test")
            self.assertIn("Наряд на калибровку MS42", note)
            self.assertIn("REQUIRES_DATA", note)       # ignition/fueling/idle/pops
            self.assertIn("limits", note)

    def test_a_pending_limit_intent_is_not_reported_as_nothing_to_do(self):
        # 6900 rpm needs acknowledge_risk (present) and acknowledge_oil_pump
        # (absent): the limits package must ask for it instead of saying EMPTY.
        request = {"intents": [{"id": "r", "kind": "rpm_limit", "transmission": "manual",
                                "soft_rpm": 6900, "hard_rpm": 7000,
                                "acknowledge_risk": True}]}
        result = build("stage1", B28_LHD, request, policy())
        package = result.package("limits")
        self.assertEqual(STATUS_REQUIRES_DATA, package.status)
        self.assertIn("HIGH_RPM_OIL_PUMP_ACK_REQUIRED",
                      [finding["code"] for finding in package.findings])
        pending = package.requirements["pending"]
        self.assertEqual("r", pending[0]["intent"])
        self.assertEqual(6750.0, pending[0]["requirements"]["oil_pump_ceiling_rpm"])
        self.assertEqual("acknowledge_oil_pump", pending[0]["requirements"]["field"])

    def test_the_oil_pump_acknowledgement_lets_the_stage_write(self):
        request = {"intents": [{"id": "r", "kind": "rpm_limit", "transmission": "manual",
                                "soft_rpm": 6900, "hard_rpm": 7000,
                                "acknowledge_risk": True, "acknowledge_oil_pump": True}]}
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "rev.bin"
            result = build("stage1", B28_LHD, request, policy(), out=out)
            self.assertEqual(STATUS_READY, result.status)
            self.assertTrue(out.exists())
            self.assertEqual(STATUS_READY, result.package("limits").status)
            values = [change["value"] for change in result.patches
                      if change["symbol"] == "id_n_max_mt__nvs"]
            self.assertEqual([6900.0, 6900.0, 6900.0], values)
            hard = [change["value"] for change in result.patches
                    if change["symbol"] == "id_n_max_max_mt__nvs"]
            self.assertEqual([7000.0, 7000.0, 7000.0], hard)

    def test_a_written_package_does_not_hide_the_pending_intents(self):
        request = {"intents": [
            {"id": "v", "kind": "vmax", "value_kmh": 240, "hard_add_kmh": 8.0},
            {"id": "r", "kind": "rpm_limit", "transmission": "manual",
             "soft_rpm": 6900, "acknowledge_risk": True},
        ]}
        result = build("stage1", B28_LHD, request, policy())
        package = result.package("limits")
        self.assertEqual(STATUS_READY, package.status)
        codes = [finding["code"] for finding in package.findings]
        self.assertIn("LIMIT_INTENT_PENDING", codes)
        texts = " ".join(finding["message"] for finding in package.findings)
        self.assertIn("acknowledge_oil_pump", texts)

    def test_popcorn_examples_load_and_write(self):
        profile = load_profile(EXAMPLES / "popcorn.example.json")
        request = json.loads((EXAMPLES / "stage1_request_popcorn.example.json")
                             .read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "stage1_popcorn.bin"
            result = build("stage1", B28_LHD, request, policy(), popcorn=profile, out=out)
            self.assertEqual(STATUS_READY, result.status)
            self.assertTrue(out.exists())
            self.assertTrue(result.crc_valid_after)
            package = result.package("overrun_pops")
            self.assertEqual(STATUS_READY, package.status)
            self.assertEqual(84, len(package.changes))
            self.assertIn("light", package.summary)
            self.assertIn("no_catalyst", package.requirements)
            self.assertEqual(profile.road_validation_plan,
                             package.requirements["road_validation_plan"])
            symbols = {change["symbol"] for change in package.changes}
            self.assertIn("ip_n_min_accin_puc__tco", symbols)   # AC-on family too
            self.assertGreater(result.value_bytes_changed, 80)

    def test_popcorn_without_a_profile_stays_in_evidence(self):
        request = json.loads((EXAMPLES / "stage1_request_popcorn.example.json")
                             .read_text(encoding="utf-8"))
        result = build("stage1", B28_LHD, request, policy())
        package = result.package("overrun_pops")
        self.assertEqual(STATUS_REQUIRES_DATA, package.status)
        self.assertIn("road_validation_plan", " ".join(package.requirements["fields"]))
        self.assertIn("EVIDENCE_REQUIRED",
                      [finding["code"] for finding in package.findings])

    def test_a_profile_without_a_road_plan_is_refused_before_anything_is_written(self):
        payload = json.loads((EXAMPLES / "popcorn.example.json").read_text(encoding="utf-8"))
        payload.pop("road_validation_plan")
        with self.assertRaises(PatchError):
            parse_profile(payload)

    def test_the_stage_build_does_not_soften_the_band_limits(self):
        with self.assertRaises(PatchError):
            parse_profile({"kind": "popcorn",
                           "no_catalyst": "катализатора нет, машина вне дорог",
                           "thermal_watch": True,
                           "road_validation_plan": "десять минут, вторая передача, наблюдатель",
                           "intensity": "medium", "threshold_add_rpm": 2500,
                           "retard_deg": -4, "cut_delay_s": 1.0})
        self.assertEqual({"light", "medium"}, set(INTENSITY_BANDS))

    def test_the_donor_intake_package_writes_with_a_declared_manifold(self):
        request = json.loads((EXAMPLES / "stage2_request_intake.example.json")
                             .read_text(encoding="utf-8"))
        donor = load_donor(DEFAULT_DONOR_BIN)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "stage2_intake.bin"
            result = build("stage2", B28_LHD, request, policy(), donor=donor, out=out)
            self.assertEqual(STATUS_READY, result.status)
            self.assertTrue(out.exists())
            self.assertTrue(result.crc_valid_after)
            package = result.package("intake")
            self.assertEqual(STATUS_READY, package.status)
            self.assertEqual("430069", result.donor["donor"]["osid"])
            self.assertIn("id_vim_pl__n_vim__maf", result.donor["applied"])
            self.assertIn("c_maf_hys_vim", result.donor["refused"])
            self.assertIn("c_maf_max_diag", result.donor["refused"])
            for change in package.changes:
                self.assertTrue(change["evidence"].startswith("donor_ms43:"))

    def test_a_donor_reference_value_needs_the_explicit_opt_in(self):
        request = json.loads((EXAMPLES / "stage2_request_intake.example.json")
                             .read_text(encoding="utf-8"))
        request["hardware"]["use_donor_references"] = True
        donor = load_donor(DEFAULT_DONOR_BIN)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "stage2_refs.bin"
            result = build("stage2", B28_LHD, request, policy(), donor=donor, out=out)
            self.assertEqual(STATUS_READY, result.status)
            self.assertIn("c_maf_max_diag", result.donor["applied"])
            values = [change["value"] for change in result.package("intake").changes
                      if change["symbol"] == "c_maf_max_diag"]
            self.assertEqual([890.0], values)

    def test_without_a_donor_the_intake_package_asks_for_one(self):
        request = json.loads((EXAMPLES / "stage2_request_intake.example.json")
                             .read_text(encoding="utf-8"))
        result = build("stage2", B28_LHD, request, policy())
        package = result.package("intake")
        self.assertEqual(STATUS_REQUIRES_DATA, package.status)
        self.assertIn("DONOR_REQUIRED", [finding["code"] for finding in package.findings])
        self.assertIn("--donor-bin", package.requirements["how_to"])
        self.assertIsNone(result.donor)

    def test_a_donor_without_a_declared_intake_is_refused(self):
        request = json.loads((EXAMPLES / "stage2_request_intake.example.json")
                             .read_text(encoding="utf-8"))
        request["hardware"].pop("intake")
        donor = load_donor(DEFAULT_DONOR_BIN)
        result = build("stage2", B28_LHD, request, policy(), donor=donor)
        package = result.package("intake")
        self.assertEqual(STATUS_REQUIRES_DATA, package.status)
        self.assertIn("HARDWARE_INTAKE_REQUIRED",
                      [finding["code"] for finding in package.findings])
        self.assertEqual("430069", result.donor["donor"]["osid"])

    def test_an_unknown_donor_file_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            bogus = Path(directory) / "bogus.bin"
            bogus.write_bytes(b"\x00" * (512 * 1024))
            with self.assertRaises(DonorError):
                load_donor(bogus)

    def test_the_delivery_note_names_the_donor_and_the_popcorn_gate(self):
        from MS42.tools.delivery_note import build_note

        request = json.loads((EXAMPLES / "stage2_request_intake.example.json")
                             .read_text(encoding="utf-8"))
        donor = load_donor(DEFAULT_DONOR_BIN)
        result = build("stage2", B28_LHD, request, policy(), donor=donor)
        note = build_note(generation_payload(result), None, "Test")
        self.assertIn("C769Y548", note)
        self.assertIn("donor_ms43:", note)
        self.assertIn("6750", note)

    def test_injector_profile_and_maf_examples_load(self):
        with tempfile.TemporaryDirectory():
            profile = load_injector_profile(EXAMPLES / "injectors.example.json")
            self.assertEqual("example template, not a datasheet", profile.source)
            curve = load_maf_curve(EXAMPLES / "maf_curve.example.json")
            self.assertEqual(256, len(curve.values))


class Variant1Tests(unittest.TestCase):
    """Октан-профиль и опции варианта 1 на MS42 (шаг 1.4 задания).

    Профиль топлива — выбор клиента: RON98 разрешает добавку угла (её выдаёт
    пакет ignition и только с логом детонации), RON95/RON91 оставляют карты
    стоковыми. Опции (DISA off, ISA off, вентилятор/термостат) честно ждут
    данных с конкретным перечнем; launch control для MS42 не существует.
    """

    def test_every_variant1_symbol_exists_in_the_definition(self):
        from MS42.tools.xdf import load_definition
        objects, _problems = load_definition(DEFAULT_XDF)
        for package in packages_for(1):
            if package.kind not in ("octane", "option"):
                continue
            for symbol in package.symbols:
                self.assertIn(symbol, objects, f"{package.id}: {symbol}")

    def test_ron98_profile_is_reported_and_allows_the_addition(self):
        request = stage1_request(octane="ron98")
        result = build("stage1", B20, request, policy())
        self.assertEqual(STATUS_READY, result.status)
        self.assertEqual("ron98", result.octane["profile"])
        self.assertTrue(result.octane["add_allowed"])
        package = result.package("octane")
        self.assertEqual(STATUS_READY, package.status)
        self.assertIn("RON98", package.summary)
        self.assertEqual("ron98", build_payload(result)["octane"]["profile"])

    def test_ron95_small_addition_is_gated_by_the_log_not_refused(self):
        """Раунд 4 (§2.1): RON95 имеет право на +1,5° без логов, поэтому
        намерение 2,0° больше не отказ профиля — это «нужен лог детонации»
        (пакет ignition в MS42 и так только по логу)."""
        request = stage1_request(octane="ron95")
        request["intents"].append({"id": "i1", "kind": "ignition", "delta_deg": 2.0})
        result = build("stage1", B20, request, policy())
        codes = [item["code"] for item in result.findings]
        self.assertNotIn("OCTANE_PROFILE_NO_ADDITION", codes)
        package = result.package("octane")
        self.assertEqual(STATUS_READY, package.status)
        self.assertIn("OCTANE_ADDITION_GATED",
                      [item["code"] for item in package.findings])

    def test_ron95_above_stage_two_right_is_refused(self):
        """5,01° — не «принеси лог», а явное превышение права профиля
        (порог Stage 2 с логом); файл не выпускается."""
        request = stage1_request(octane="ron95")
        request["intents"].append({"id": "i1", "kind": "ignition", "delta_deg": 5.01})
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out.bin"
            result = build("stage1", B20, request, policy(), out=out)
            self.assertEqual(STATUS_BLOCKED, result.status)
            self.assertIn("OCTANE_PROFILE_LIMIT",
                          [item["code"] for item in result.findings])
            self.assertFalse(out.exists())
            self.assertEqual([], result.patches)

    def test_ron91_still_refuses_any_requested_ignition_addition(self):
        request = stage1_request(octane="ron91")
        request["intents"].append({"id": "i1", "kind": "ignition", "delta_deg": 0.75})
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out.bin"
            result = build("stage1", B20, request, policy(), out=out)
            self.assertEqual(STATUS_BLOCKED, result.status)
            self.assertIn("OCTANE_PROFILE_NO_ADDITION",
                          [item["code"] for item in result.findings])
            self.assertFalse(out.exists())
            self.assertEqual([], result.patches)

    def test_ron91_keeps_the_stock_maps_and_says_so(self):
        result = build("stage1", B20, stage1_request(octane="ron91"), policy())
        package = result.package("octane")
        self.assertEqual(STATUS_READY, package.status)
        self.assertFalse(package.requirements["add_allowed"])
        self.assertIn("OCTANE_STOCK_MAPS",
                      [item["code"] for item in package.findings])

    def test_an_unknown_or_conflicting_profile_is_refused(self):
        for request, code in (
            (stage1_request(octane="ron100"), "OCTANE_PROFILE_UNKNOWN"),
            ({"intents": [{"id": "o", "kind": "octane", "fuel": "ron95"}],
              "octane": "ron98"},
             "OCTANE_PROFILE_CONFLICT"),
        ):
            with self.subTest(code=code):
                result = build("stage1", B20, request, policy())
                self.assertEqual(STATUS_BLOCKED, result.status)
                self.assertIn(code, [item["code"] for item in result.findings])
                self.assertEqual([], result.packages)

    def test_a_missing_profile_waits_for_the_customer(self):
        result = build("stage1", B20, stage1_request(), policy())
        package = result.package("octane")
        self.assertEqual(STATUS_REQUIRES_DATA, package.status)
        self.assertEqual(["octane_profile"], package.requirements["needs"])
        self.assertIn("OCTANE_PROFILE_REQUIRED",
                      [item["code"] for item in package.findings])

    def test_the_octane_intent_can_come_from_the_request(self):
        request = {"octane": "ron98",
                   "intents": [{"id": "o1", "kind": "octane", "fuel": "ron98"},
                               {"id": "v1", "kind": "vmax", "value_kmh": 240,
                                "hard_add_kmh": 8.0}]}
        result = build("stage1", B20, request, policy())
        self.assertEqual(STATUS_READY, result.status)
        self.assertIn("o1", result.package("octane").summary)

    def test_options_wait_for_data_with_concrete_needs(self):
        request = stage1_request()
        request["intents"] += [{"id": "d1", "kind": "disa_off"},
                               {"id": "s1", "kind": "isa_off"},
                               {"id": "f1", "kind": "fan_thermostat"}]
        result = build("stage1", B20, request, policy())
        self.assertEqual(STATUS_READY, result.status)          # ограничители записаны
        for package_id in ("disa_off", "isa_off", "fan_thermostat"):
            package = result.package(package_id)
            self.assertEqual(STATUS_REQUIRES_DATA, package.status, package_id)
            self.assertTrue(package.requirements["requested"], package_id)
            self.assertGreaterEqual(len(package.requirements["needs"]), 3, package_id)
            self.assertTrue(package.requirements["why"], package_id)
            self.assertIn("запрошено клиентом", package.summary)

    def test_options_are_reported_even_when_not_requested(self):
        result = build("stage1", B20, stage1_request(), policy())
        for package_id in ("disa_off", "isa_off", "fan_thermostat"):
            package = result.package(package_id)
            self.assertEqual(STATUS_REQUIRES_DATA, package.status, package_id)
            self.assertFalse(package.requirements["requested"])
            self.assertIn("не запрошена", package.summary)

    def test_launch_control_is_refused_with_a_reason(self):
        request = stage1_request()
        request["intents"].append({"id": "l1", "kind": "launch"})
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out.bin"
            result = build("stage1", B20, request, policy(), out=out)
            self.assertEqual(STATUS_BLOCKED, result.status)
            findings = [item for item in result.findings
                        if item["code"] == "INTENT_UNAVAILABLE"]
            self.assertEqual(1, len(findings))
            self.assertIn("launch control", findings[0]["message"])
            self.assertFalse(out.exists())

    def test_the_variant1_example_request_is_a_valid_input(self):
        request = json.loads((EXAMPLES / "stage1_request_variant1.example.json")
                             .read_text(encoding="utf-8"))
        result = build("stage1", B20, request, policy())
        self.assertEqual(STATUS_READY, result.status)
        self.assertEqual("ron98", result.octane["profile"])
        self.assertTrue(result.package("disa_off").requirements["requested"])
        self.assertEqual("stage1", build_payload(result)["stage"])


if __name__ == "__main__":
    unittest.main()

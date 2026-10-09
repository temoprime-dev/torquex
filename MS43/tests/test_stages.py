"""Тесты генератора MS43 (`MS43/tools/stages.py`)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from MS43.tools import checksum as ck
from MS43.tools import patches as pt
from MS43.tools import stages
from MS43.tools.policy import load_policy
from tools import variant1 as v1  # noqa: E402

FIRMWARE = Path(__file__).resolve().parents[2] / "MS43" / "firmware"
REFERENCE = FIRMWARE / "Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin"

VMAX_INTENT = {"id": "v1", "kind": "vmax", "transmission": "manual", "value_kmh": 250,
               "hard_add_kmh": 5.0, "acknowledge_risk": True}
RPM_INTENT = {"id": "r1", "kind": "rpm_limit", "transmission": "manual",
              "soft_rpm": 6688, "hard_rpm": 6720, "acknowledge_risk": True}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.image = REFERENCE.read_bytes()
        cls.policy = load_policy()
        cls.catalog = pt.load_catalog()

    def build(self, stage: str, intents, **kwargs):
        request = {"stage": stage, "customer": "тест", "intents": list(intents)}
        # Правовая справка (docs/TASK_VARIANT1_RU.md §2) проверяется отдельным
        # тестом; здесь ставим подтверждение, чтобы тесты проверяли сам пакет.
        if any(item.get("kind") in v1.LEGAL_GATE_KINDS for item in request["intents"]):
            request.setdefault("acknowledge_legality", True)
        return stages.build(
            stage,
            self.image,
            request,
            policy=self.policy,
            patch_catalog=self.catalog,
            **kwargs,
        )

    def codes(self, result) -> list[str]:
        return [item["code"] for item in result.findings]

    def verify(self, image: bytes) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.bin"
            path.write_bytes(image)
            self.assertTrue(ck.verify_image(path).ok)


class LimitsTests(Base):
    def test_stage1_limits_are_written(self) -> None:
        result = self.build("stage1", [VMAX_INTENT, RPM_INTENT])
        self.assertEqual("BUILT", result.status)
        self.verify(result.result.image)
        written = {item.symbol for item in result.plan.symbols if item.cells}
        self.assertIn("id_n_max_mt__gear", written)
        self.assertIn("id_n_max_max_mt__gear", written)

    def test_risk_acknowledgement_is_required(self) -> None:
        intent = dict(RPM_INTENT)
        intent.pop("acknowledge_risk")
        result = self.build("stage1", [intent])
        self.assertEqual("REJECTED", result.status)
        self.assertIn("ACKNOWLEDGE_RISK_REQUIRED", self.codes(result))

    def test_oil_pump_acknowledgement_is_required(self) -> None:
        intent = {"id": "r1", "kind": "rpm_limit", "transmission": "manual",
                  "soft_rpm": 6800, "acknowledge_risk": True, "acknowledge_oil_pump": False}
        result = self.build("stage1", [intent])
        self.assertEqual("REJECTED", result.status)
        self.assertIn("ACKNOWLEDGE_OIL_PUMP_REQUIRED", self.codes(result))
        ok = {"id": "r1", "kind": "rpm_limit", "transmission": "manual",
              "soft_rpm": 6800, "acknowledge_risk": True, "acknowledge_oil_pump": True}
        result = self.build("stage1", [ok])
        self.assertEqual("BUILT", result.status)
        self.assertIn("acknowledge_oil_pump", result.plan.acknowledgements)

    def test_hard_below_soft_is_refused(self) -> None:
        intent = {"id": "r1", "kind": "rpm_limit", "transmission": "manual",
                  "soft_rpm": 6800, "hard_rpm": 6600, "acknowledge_risk": True,
                  "acknowledge_oil_pump": True}
        result = self.build("stage1", [intent])
        self.assertEqual("REJECTED", result.status)
        self.assertIn("HARD_BELOW_SOFT", self.codes(result))

    def test_empty_request_writes_nothing(self) -> None:
        result = self.build("stage1", [])
        self.assertEqual("REQUIRES_DATA", result.status)
        self.assertIsNone(result.result)
        self.assertIn("NOTHING_TO_DO", self.codes(result))


class FeatureTests(Base):
    def test_launch_control_writes_patch_and_cells(self) -> None:
        result = self.build(
            "stage1",
            [{"id": "l1", "kind": "launch_control", "mode": 2, "rpm": 4000, "vmax_kmh": 30},
             {"id": "l2", "kind": "ral", "pvs_min": 60}],
        )
        self.assertEqual("BUILT", result.status)
        self.verify(result.result.image)
        self.assertIn(stages.LC_TITLE, result.result.patches)
        self.assertEqual(2, result.result.image[0x71319])   # c_conf_lc = режим 2
        self.assertEqual(125, result.result.image[0x7131B])  # 4000 об/мин
        self.assertEqual(30, result.result.image[0x7131C])
        self.assertEqual(1, result.result.image[0x71322])   # c_conf_ral

    def test_nls_is_refused(self) -> None:
        result = self.build("stage1", [{"id": "n1", "kind": "nls"}])
        self.assertEqual("REJECTED", result.status)
        self.assertIn("NLS_NOT_IMPLEMENTED", self.codes(result))

    def test_mil_knock_and_cluster_write_their_patches(self) -> None:
        result = self.build(
            "stage1",
            [{"id": "m1", "kind": "mil_knock", "seconds": 1.5},
             {"id": "m2", "kind": "m_cluster"},
             {"id": "d1", "kind": "ds2_logging"}],
        )
        self.assertEqual("BUILT", result.status)
        self.verify(result.result.image)
        for title in (stages.MIL_TITLE, stages.MCLUSTER_TITLE, stages.DS2_TITLE):
            self.assertIn(title, result.result.patches)
        self.assertEqual(0x03, result.result.image[0x707D0])  # оба уровня: флаги дал патч
        self.assertEqual(0x96, result.result.image[0x707D1])  # 1.5 с


class PopcornTests(Base):
    PROFILE = {
        "band": "light",
        "rpm_add": 416,
        "angle": -6.0,
        "no_catalyst": "катализатор удалён, машина вне дорог",
        "thermal_watch": True,
        "road_validation_plan": "2-я и 3-я передачи, 10 минут, следить за EGT",
    }

    def build_popcorn(self, profile):
        return self.build("stage1", [{"id": "p1", "kind": "popcorn", "band": "light"}],
                          popcorn_profile=profile)

    def test_light_band_is_written(self) -> None:
        result = self.build_popcorn(self.PROFILE)
        self.assertEqual("BUILT", result.status)
        self.verify(result.result.image)
        written = {item.symbol for item in result.plan.symbols if item.cells}
        self.assertIn("c_n_hys_max_puc", written)
        self.assertIn("ip_iga_puc__n__tco", written)

    def test_aggressive_band_is_refused(self) -> None:
        result = self.build_popcorn({**self.PROFILE, "band": "aggressive"})
        self.assertEqual("REJECTED", result.status)
        self.assertIn("POPCORN_AGGRESSIVE_REFUSED", self.codes(result))

    def test_missing_declarations_are_refused(self) -> None:
        for key, code in (
            ("no_catalyst", "POPCORN_NO_CATALYST_REQUIRED"),
            ("thermal_watch", "POPCORN_THERMAL_WATCH_REQUIRED"),
            ("road_validation_plan", "POPCORN_ROAD_PLAN_REQUIRED"),
        ):
            profile = dict(self.PROFILE)
            profile.pop(key)
            result = self.build_popcorn(profile)
            self.assertEqual("REJECTED", result.status, key)
            self.assertIn(code, self.codes(result), key)

    def test_values_outside_the_band_are_refused(self) -> None:
        result = self.build_popcorn({**self.PROFILE, "rpm_add": 1500})
        self.assertEqual("REJECTED", result.status)
        self.assertIn("POPCORN_BAND_VIOLATION", self.codes(result))
        result = self.build_popcorn({**self.PROFILE, "angle": -20.0})
        self.assertEqual("REJECTED", result.status)
        self.assertIn("POPCORN_BAND_VIOLATION", self.codes(result))

    def test_medium_band_allows_more(self) -> None:
        result = self.build_popcorn({**self.PROFILE, "band": "medium", "angle": -12.0})
        self.assertEqual("BUILT", result.status, self.codes(result))
        self.verify(result.result.image)


class Stage2Tests(Base):
    def test_stage2_needs_hardware(self) -> None:
        request = {"stage": "stage2", "customer": "тест", "intents": [VMAX_INTENT]}
        result = stages.build(
            "stage2", self.image, request, policy=self.policy, patch_catalog=self.catalog
        )
        self.assertEqual("REJECTED", result.status)
        self.assertIn("HARDWARE_REQUIRED", self.codes(result))

    def test_stage2_with_hardware_but_without_data(self) -> None:
        request = {
            "stage": "stage2",
            "customer": "тест",
            "hardware": {"chassis": "E46", "engine": "M54B30", "intake": "M50 manifold"},
            "intents": [VMAX_INTENT, {"id": "i1", "kind": "intake"}, {"id": "t1", "kind": "torque"}],
        }
        result = stages.build(
            "stage2", self.image, request, policy=self.policy, patch_catalog=self.catalog
        )
        self.assertEqual("REQUIRES_DATA", result.status)
        pending = {item.id: item for item in result.packages if item.status == "requires_data"}
        self.assertEqual({"intake", "torque"}, set(pending))
        self.assertTrue(all(item.needs for item in pending.values()))
        self.assertEqual("E46", result.manifest["hardware"]["chassis"])

    def test_requires_data_packages_explain_themselves(self) -> None:
        # ignition с добавкой выше порога без лога детонации и ванос со сдвигом
        # без лога фаз — оба обязаны ответить REQUIRES_DATA со списком замеров
        result = self.build(
            "stage1",
            [
                {"id": "f1", "kind": "full_load"},
                {"id": "g1", "kind": "ignition", "delta_deg": 2.5},
                {"id": "v1", "kind": "vanos", "shift_in_deg": 2.0},
            ],
        )
        self.assertEqual("REQUIRES_DATA", result.status)
        pending = {item.id for item in result.packages if item.status == "requires_data"}
        self.assertEqual({"full_load", "ignition", "vanos"}, pending)
        for package in result.packages:
            if package.status == "requires_data":
                self.assertTrue(package.needs, package.id)
                self.assertTrue(all(len(need) > 10 for need in package.needs), package.id)


class ManifestAndNoteTests(Base):
    def test_manifest_and_note_content(self) -> None:
        result = self.build("stage1", [VMAX_INTENT, RPM_INTENT,
                                       {"id": "l1", "kind": "launch_control", "mode": 1}])
        self.assertEqual("BUILT", result.status)
        manifest = result.manifest
        self.assertEqual("stage1", manifest["stage"])
        self.assertEqual("BUILT", manifest["status"])
        self.assertIn(stages.LC_TITLE, manifest["patches"])
        self.assertEqual(result.result.sha256, manifest["result_sha256"])
        self.assertTrue(manifest["crc_words"])
        note = stages.build_note(result, REFERENCE.name)
        self.assertIn("Наряд MS43", note)
        self.assertIn(stages.LC_TITLE, note)
        self.assertIn("не обещается", note)          # «+X л.с.» не обещаем
        self.assertIn("bootmode не нужен", note)
        self.assertIn(result.result.sha256[:16], note)
        for finding in result.findings:
            self.assertIn(finding["code"], note)

    def test_note_lists_pending_packages(self) -> None:
        # сдвиг без лога фаз — пакет ждёт данные, наряд обязан их напечатать
        result = self.build("stage1", [{"id": "f1", "kind": "vanos", "shift_in_deg": 2.0}])
        note = stages.build_note(result, REFERENCE.name)
        self.assertIn("VANOS", note)
        self.assertIn("нужно:", note)


class VerifyManifestTests(Base):
    """Проверка выпущенного файла по манифесту (шаг 1.3 задания)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory(prefix="ms43_verify_")
        cls.dir = Path(cls.tmp.name)
        cls.out = cls.dir / "stage1.bin"
        cls.manifest = cls.dir / "manifest.json"
        request = cls.dir / "request.json"
        request.write_text(
            json.dumps({"stage": "stage1", "customer": "тест",
                        "intents": [RPM_INTENT]}),
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            code = stages.main(
                ["--stage", "stage1", "--bin", str(REFERENCE), "--request", str(request),
                 "--out", str(cls.out), "--report", str(cls.manifest)]
            )
        assert code == 0, code

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_verified_file(self) -> None:
        verification = stages.verify_manifest(self.manifest)
        self.assertEqual("VERIFIED", verification.status, verification.as_dict())
        self.assertTrue(verification.ok)
        self.assertEqual(str(self.out), verification.image_path)
        self.assertEqual(str(REFERENCE), verification.source_path)
        names = {item.name: item.ok for item in verification.checks}
        for name in ("manifest_version", "status", "sha256", "checksums", "image_kind",
                     "cal_version", "crc_words", "planned_cells", "changed_bytes",
                     "outside_plan_changes"):
            self.assertTrue(names[name], (name, verification.as_dict()))

    def test_markdown_report(self) -> None:
        text = stages.verification_as_markdown(stages.verify_manifest(self.manifest))
        self.assertIn("# Проверка файла по манифесту (MS43)", text)
        self.assertIn("| sha256 | OK |", text)
        self.assertIn("соответствует манифесту", text)

    def test_tampered_planned_cell_is_caught(self) -> None:
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        cell = manifest["plan"]["symbols"][0]["cells"][0]
        data = bytearray(self.out.read_bytes())
        data[int(cell["address"], 16)] ^= 0x01
        tampered = self.dir / "tampered_cell.bin"
        tampered.write_bytes(bytes(data))
        verification = stages.verify_manifest(self.manifest, tampered)
        self.assertEqual("FAILED", verification.status)
        failed = {item.name for item in verification.checks if not item.ok}
        self.assertIn("sha256", failed)
        self.assertIn("planned_cells", failed)

    def test_tampered_byte_outside_plan_is_caught(self) -> None:
        data = bytearray(self.out.read_bytes())
        data[0x100] ^= 0xFF  # внутри образа, но вне плана и вне слов CRC
        tampered = self.dir / "tampered_region.bin"
        tampered.write_bytes(bytes(data))
        verification = stages.verify_manifest(self.manifest, tampered)
        self.assertEqual("FAILED", verification.status)
        failed = {item.name for item in verification.checks if not item.ok}
        self.assertIn("outside_plan_changes", failed)

    def test_missing_crc_repair_is_caught(self) -> None:
        """Правка без пересчёта CRC: суммы не сходятся — файл прошивать нельзя."""
        data = bytearray(self.out.read_bytes())
        cell = json.loads(self.manifest.read_text(encoding="utf-8"))["plan"]["symbols"][0]["cells"][0]
        address = int(cell["address"], 16)
        data[address] = int(cell["before_raw"]) & 0xFF  # вернули стоковое значение
        broken = self.dir / "crc_broken.bin"
        broken.write_bytes(bytes(data))
        verification = stages.verify_manifest(self.manifest, broken)
        self.assertEqual("FAILED", verification.status)
        failed = {item.name for item in verification.checks if not item.ok}
        self.assertIn("checksums", failed)

    def test_other_image_is_not_accepted(self) -> None:
        other = FIRMWARE / "Siemens_MS43_MS430069_E46_M54B30_EU4_RHD.bin"
        if not other.exists():
            self.skipTest("нет второго стока 430069")
        verification = stages.verify_manifest(self.manifest, other)
        self.assertEqual("FAILED", verification.status)
        failed = {item.name for item in verification.checks if not item.ok}
        self.assertIn("sha256", failed)

    def test_manifest_without_result_name_needs_explicit_file(self) -> None:
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        data.pop("result_name", None)
        path = self.dir / "no_result_name.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        verification = stages.verify_manifest(path)
        self.assertEqual("FAILED", verification.status)
        self.assertIn("result_name", {item.name for item in verification.checks})

    def test_cli_verify_exit_codes(self) -> None:
        report = Path(self.tmp.name) / "verify.md"
        with contextlib.redirect_stdout(io.StringIO()):
            code = stages.main(["--verify-manifest", str(self.manifest),
                                "--note-out", str(report)])
        self.assertEqual(0, code)
        self.assertIn("Проверка файла по манифесту", report.read_text(encoding="utf-8"))
        tampered = Path(self.tmp.name) / "bad.bin"
        data = bytearray(self.out.read_bytes())
        data[0x100] ^= 0xFF
        tampered.write_bytes(bytes(data))
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = stages.main(["--verify-manifest", str(self.manifest),
                                "--image", str(tampered)])
        self.assertEqual(2, code)

    def test_requires_data_manifest_is_not_built(self) -> None:
        request = self.dir / "vanos_request.json"
        request.write_text(
            json.dumps({"stage": "stage1", "intents": [
                {"id": "v1", "kind": "vanos", "shift_in_deg": 2.0}]}),
            encoding="utf-8",
        )
        manifest = self.dir / "requires_data.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = stages.main([
                "--stage", "stage1", "--bin", str(REFERENCE), "--request", str(request),
                "--report", str(manifest),
            ])
        # REQUIRES_DATA — не отказ заявки: файла нет, и проверке нечего делать.
        self.assertEqual(0, code)
        verification = stages.verify_manifest(manifest)
        self.assertEqual("NOT_BUILT", verification.status)
        self.assertFalse(verification.ok)
        with contextlib.redirect_stdout(io.StringIO()):
            code = stages.main(["--verify-manifest", str(manifest)])
        self.assertEqual(0, code)


class SweepTests(Base):
    """Полный набор фич на всём корпусе стоков."""

    REQUEST = {
        "stage": "stage1",
        "customer": "sweep",
        "hardware": {"chassis": "E46", "engine": "M54B30"},
        "intents": [
            {"id": "v1", "kind": "vmax", "transmission": "manual", "value_kmh": 250,
             "hard_add_kmh": 5.0, "acknowledge_risk": True},
            {"id": "r1", "kind": "rpm_limit", "transmission": "manual",
             "soft_rpm": 6688, "hard_rpm": 6720, "acknowledge_risk": True},
            {"id": "l1", "kind": "launch_control", "mode": 1, "rpm": 4000, "vmax_kmh": 30},
            {"id": "l2", "kind": "ral", "pvs_min": 60},
            {"id": "m1", "kind": "mil_knock", "seconds": 1.5},
            {"id": "m2", "kind": "m_cluster"},
            {"id": "d1", "kind": "ds2_logging"},
            {"id": "x1", "kind": "vss_fix"},
            {"id": "f1", "kind": "full_load"},
        ],
    }
    POPCORN = {
        "band": "medium",
        "rpm_add": 800,
        "angle": -10.0,
        "no_catalyst": "катализатор отсутствует",
        "thermal_watch": True,
        "road_validation_plan": "заезды 2-3 передачи 10 минут",
    }
    ANOMALY = "Siemens_MS43_MS430069_E39_M54B25_EU4_LHD.bin"

    def test_full_feature_set_on_every_stock(self) -> None:
        built = 0
        for path in sorted(FIRMWARE.glob("*.bin")):
            image = path.read_bytes()
            result = stages.build(
                "stage1", image, self.REQUEST, policy=self.policy,
                patch_catalog=self.catalog, popcorn_profile=self.POPCORN,
            )
            if path.name == self.ANOMALY:
                self.assertEqual("REJECTED", result.status, path.name)
                self.assertIn("SOURCE_CRC_INVALID", self.codes(result))
                continue
            self.assertEqual("BUILT", result.status, f"{path.name}: {self.codes(result)}")
            self.assertEqual(8, len(result.result.patches), path.name)
            self.verify(result.result.image)
            built += 1
        self.assertEqual(17, built)


class CliTests(Base):
    def test_cli_writes_file_manifest_and_note(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / "request.json"
            request.write_text(
                json.dumps({"stage": "stage1", "customer": "тест",
                            "intents": [VMAX_INTENT, RPM_INTENT]}),
                encoding="utf-8",
            )
            out = Path(directory) / "stage1.bin"
            report = Path(directory) / "manifest.json"
            note = Path(directory) / "note.md"
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                code = stages.main(
                    ["--stage", "stage1", "--bin", str(REFERENCE), "--request", str(request),
                     "--out", str(out), "--report", str(report), "--note", str(note)]
                )
            self.assertEqual(0, code, captured.getvalue())
            self.assertTrue(out.exists())
            self.assertTrue(ck.verify_image(out).ok)
            self.assertEqual("BUILT", json.loads(report.read_text(encoding="utf-8"))["status"])
            self.assertIn("Наряд MS43", note.read_text(encoding="utf-8"))

    def test_cli_rejected_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / "request.json"
            request.write_text(
                json.dumps({"stage": "stage1", "intents": [{"id": "n1", "kind": "nls"}]}),
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                code = stages.main(
                    ["--stage", "stage1", "--bin", str(REFERENCE), "--request", str(request)]
                )
            self.assertEqual(2, code)

    def test_example_requests_build(self) -> None:
        """Задание 2 ревью: ВСЕ примеры заявок собираются «как есть»,
        включая v2 и v3 (доказательства приложены рядом, выданы анализатором).
        Запуск из каталога примеров — пути к доказательствам относительные."""
        examples = Path(stages.__file__).resolve().parents[1] / "examples"
        old_cwd = os.getcwd()
        os.chdir(examples)
        try:
            names = sorted(path.name for path in examples.glob("*.example.json")
                           if path.name != "popcorn.example.json")
            # popcorn.example.json — профиль для --popcorn, не заявка;
            # пустая заявка намеренно отказана (см. её notes)
            names.remove("stage1_request_empty.example.json")
            self.assertGreaterEqual(len(names), 6)
            for name in names:
                request = json.loads((examples / name).read_text(encoding="utf-8"))
                stage = request.get("stage", "stage1")
                result = stages.build(
                    stage, self.image, request, policy=self.policy,
                    patch_catalog=self.catalog,
                )
                self.assertIn(result.status, {"BUILT", "REQUIRES_DATA"}, name)
                self.assertNotEqual("REJECTED", result.status,
                                    (name, self.codes(result)))
        finally:
            os.chdir(old_cwd)

    def test_v2_v3_examples_write_expected_packages(self) -> None:
        examples = Path(stages.__file__).resolve().parents[1] / "examples"
        old_cwd = os.getcwd()
        os.chdir(examples)
        try:
            request = json.loads(
                (examples / "stage1_variant_v2.example.json").read_text(encoding="utf-8"))
            result = stages.build("stage1", self.image, request, policy=self.policy,
                                  patch_catalog=self.catalog)
            self.assertEqual("BUILT", result.status, self.codes(result))
            self.assertEqual({"ignition", "fuel", "vanos", "limits"},
                             {p.id for p in result.written_packages})
            request = json.loads(
                (examples / "stage1_variant_v3.example.json").read_text(encoding="utf-8"))
            result = stages.build("stage1", self.image, request, policy=self.policy,
                                  patch_catalog=self.catalog)
            self.assertEqual("BUILT", result.status, self.codes(result))
            written = {p.id for p in result.written_packages}
            self.assertIn("popcorn", written)
            self.assertIn("dtc", written)
        finally:
            os.chdir(old_cwd)


ARCHIVE = Path(__file__).resolve().parents[2] / "MS43" / "firmware_archive"


class CalVersionTests(Base):
    """Генератор обязан отказывать на неподдерживаемой версии (REVIEW_CLAUDE_RU.md, задание 1)."""

    def test_foreign_stock_is_rejected_before_packages(self) -> None:
        image = (ARCHIVE / "E39_530__c6668528_Ca430066_Stok.bin").read_bytes()
        result = stages.build(
            "stage1",
            image,
            {"stage": "stage1", "intents": [VMAX_INTENT]},
            policy=self.policy,
            patch_catalog=self.catalog,
        )
        self.assertEqual("REJECTED", result.status)
        self.assertIn("CAL_VERSION_UNSUPPORTED", self.codes(result))
        self.assertEqual("430066", result.cal_version)
        self.assertEqual(("430069",), result.software)
        self.assertEqual((), result.packages)  # до пакетов дело не дошло
        self.assertEqual("430066", result.manifest["cal_version"])
        self.assertEqual(["430069"], result.manifest["supported_software"])
        self.assertIsNone(result.result)  # ничего не записано

    def test_64k_read_is_rejected_with_its_version(self) -> None:
        image = (ARCHIVE / "E46_330i__c464650r_Ca430064_Stok_Cal.bin").read_bytes()
        result = stages.build(
            "stage1", image, {"stage": "stage1", "intents": [VMAX_INTENT]},
            policy=self.policy, patch_catalog=self.catalog,
        )
        self.assertEqual("REJECTED", result.status)
        self.assertIn("IMAGE_SIZE", self.codes(result))
        self.assertEqual("430064", result.cal_version)

    def test_supported_image_states_versions_in_note_and_manifest(self) -> None:
        result = self.build("stage1", [VMAX_INTENT, RPM_INTENT])
        self.assertEqual("BUILT", result.status)
        self.assertEqual("430069", result.cal_version)
        self.assertEqual(["430069"], result.manifest["supported_software"])
        note = stages.build_note(result, "stock.bin")
        self.assertIn("версия калибровки образа: 430069", note)
        self.assertIn("поддерживаемые версии", note)

    def test_cli_refuses_a_foreign_stock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # версия без политики: 430055 теперь покрыта, поэтому берём 430099
            # (подменённая строка версии, CRC пересчитаны — отказ именно по версии)
            source = Path(directory) / "foreign.bin"
            broken = bytearray(self.image)
            broken[0x70008:0x7000E] = b"430099"
            raw = Path(directory) / "raw.bin"
            raw.write_bytes(bytes(broken))
            ck.repair_image(raw, source)
            request = Path(directory) / "request.json"
            request.write_text(
                json.dumps({"stage": "stage1", "intents": [VMAX_INTENT]}), encoding="utf-8"
            )
            out = Path(directory) / "out.bin"
            with contextlib.redirect_stdout(io.StringIO()):
                code = stages.main([
                    "--stage", "stage1", "--bin", str(source),
                    "--request", str(request), "--out", str(out),
                ])
            self.assertEqual(2, code)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()

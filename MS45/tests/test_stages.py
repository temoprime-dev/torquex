"""Тесты генератора MS45 (``MS45/tools/stages.py``): Stage 1/2, пакеты, наряд.

Проверяется весь маршрут задания: заявка клиента -> пакеты -> план ядра -> файл ->
наряд. Отдельно проверяются ворота продукта: подтверждения, запрет агрессивных
хлопков, Stage 2 только с железом, launch control как «нужны данные» и отсутствие
обещаний прироста мощности.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from MS45.tools import checksums as ck
from MS45.tools import stages
from MS45.tests.support import unknown_family_image  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
MS45 = ROOT / "MS45"
EXAMPLES = MS45 / "examples"
BO00_CAL = MS45 / "firmware" / "MS45.0_456BO00S_E83" / "7561448_0044560BO00S0CO0R.bin"
LO00_CAL = MS45 / "firmware_spdaten" / "cal" / "MS45.1_0044570LO00S01J2R_P7561517_cal_116K.bin"
BN00_CAL = MS45 / "firmware_spdaten" / "cal" / "MS45.0_0044560BN00S07F0G_P7550088_cal_116K.bin"
BL00_CAL = MS45 / "firmware_spdaten" / "cal" / "MS45.0_0044560BL00S0IA3N_P7537853_cal_116K.bin"

STAGE1 = json.loads((EXAMPLES / "stage1_request.example.json").read_text(encoding="utf-8"))
STAGE2 = json.loads((EXAMPLES / "stage2_request_intake.example.json").read_text(encoding="utf-8"))


def _codes(result: stages.StageResult) -> list[str]:
    return [finding["code"] for finding in result.findings]


class TestStage1(unittest.TestCase):
    """Stage 1: ограничители и хлопки."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.image = BO00_CAL.read_bytes()

    def test_example_builds(self) -> None:
        result = stages.build("stage1", self.image, STAGE1)
        self.assertEqual(result.status, "BUILT", _codes(result))
        self.assertEqual(result.family, "4560BO00S")
        self.assertEqual({p.id for p in result.written_packages}, {"limits", "pops"})
        self.assertIsNotNone(result.result)
        assert result.result is not None
        self.assertTrue(ck.verify_bytes(result.result.image).ok)

    def test_only_planned_cells_and_checksums_change(self) -> None:
        result = stages.build("stage1", self.image, STAGE1)
        assert result.plan is not None and result.result is not None
        planned = {
            offset
            for cell in result.plan.planned_cells
            for offset in range(cell.image_offset, cell.image_offset + cell.size_bytes)
        }
        changed = {
            index
            for index, (a, b) in enumerate(zip(self.image, result.result.image))
            if a != b
        }
        self.assertTrue(changed)
        self.assertLessEqual(changed, planned | set(result.plan.checksum_cells))

    def test_signature_untouched(self) -> None:
        result = stages.build("stage1", self.image, STAGE1)
        assert result.result is not None
        self.assertEqual(result.result.image[0x170:0x1B4], self.image[0x170:0x1B4])

    def test_pops_requires_no_catalyst_and_temperature(self) -> None:
        request = json.loads(json.dumps(STAGE1))
        request.pop("no_catalyst")
        result = stages.build("stage1", self.image, request)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("ACKNOWLEDGE_REQUIRED", _codes(result))
        request = json.loads(json.dumps(STAGE1))
        request.pop("acknowledge_temperature")
        result = stages.build("stage1", self.image, request)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("ACKNOWLEDGE_REQUIRED", _codes(result))

    def test_pops_aggressive_profile_forbidden(self) -> None:
        request = json.loads(json.dumps(STAGE1))
        request["intents"][2]["profile"] = "aggressive"
        result = stages.build("stage1", self.image, request)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("POPS_PROFILE_FORBIDDEN", _codes(result))

    def test_pops_delay_out_of_range(self) -> None:
        request = json.loads(json.dumps(STAGE1))
        request["intents"][2]["delay_s"] = 5.0
        result = stages.build("stage1", self.image, request)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("POLICY_RANGE_VIOLATION", _codes(result))

    def test_rev_limit_above_stock_needs_risk_ack(self) -> None:
        request = json.loads(json.dumps(STAGE1))
        request["intents"][1].pop("acknowledge_risk")
        result = stages.build("stage1", self.image, request)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("ACKNOWLEDGE_REQUIRED", _codes(result))
        self.assertTrue(any("acknowledge_risk" in f["message"] for f in result.findings))

    def test_rev_limit_above_damper_threshold(self) -> None:
        request = json.loads(json.dumps(STAGE1))
        request["intents"][1]["value"] = 6950
        result = stages.build("stage1", self.image, request)
        self.assertEqual(result.status, "REJECTED")
        self.assertTrue(any("acknowledge_damper" in f["message"] for f in result.findings))
        request["intents"][1]["acknowledge_damper"] = True
        result = stages.build("stage1", self.image, request)
        self.assertEqual(result.status, "BUILT", _codes(result))

    def test_rev_limit_above_core_policy_refused(self) -> None:
        request = json.loads(json.dumps(STAGE1))
        request["intents"][1].update({"value": 7500, "acknowledge_risk": True, "acknowledge_damper": True})
        result = stages.build("stage1", self.image, request)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("POLICY_RANGE_VIOLATION", _codes(result))

    def test_transmission_variants(self) -> None:
        for transmission, symbols in (
            ("at", ("id_n_max_at", "id_n_max_h_at")),
            ("amt", ("id_n_max_amt", "id_n_max_h_amt")),
        ):
            request = {"intents": [
                {"kind": "rev_limit", "value": 6600, "transmission": transmission, "acknowledge_risk": True}
            ]}
            result = stages.build("stage1", self.image, request)
            self.assertEqual(result.status, "BUILT", _codes(result))
            assert result.plan is not None
            self.assertEqual({item.symbol for item in result.plan.symbols}, set(symbols))

    def test_bad_transmission(self) -> None:
        request = {"intents": [{"kind": "rev_limit", "value": 6600, "transmission": "dsg"}]}
        result = stages.build("stage1", self.image, request)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("TRANSMISSION_INVALID", _codes(result))

    def test_missing_value(self) -> None:
        result = stages.build("stage1", self.image, {"intents": [{"kind": "vmax"}]})
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("VALUE_REQUIRED", _codes(result))

    def test_unknown_intent(self) -> None:
        result = stages.build("stage1", self.image, {"intents": [{"kind": "turbo_boost"}]})
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("INTENT_UNKNOWN", _codes(result))

    def test_no_intents(self) -> None:
        result = stages.build("stage1", self.image, {})
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("NOTHING_REQUESTED", _codes(result))

    def test_unsupported_family(self) -> None:
        """Отказ по версии: с шага 3 поддержаны все десять семейств, поэтому
        берём синтезированный чужой идентификатор (см. MS45/tests/support.py)."""
        result = stages.build("stage1", unknown_family_image(BL00_CAL.read_bytes()), STAGE1)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("CAL_VERSION_UNSUPPORTED", _codes(result))

    def test_bl00_family_is_supported(self) -> None:
        """BL00S перешёл в поддержку на шаге 3 (лимиты и ПХХ доказаны по стокам)."""
        result = stages.build("stage1", BL00_CAL.read_bytes(), STAGE1)
        self.assertEqual(result.status, "BUILT", _codes(result))
        assert result.result is not None
        self.assertTrue(ck.verify_bytes(result.result.image).ok)

    def test_lo00_family(self) -> None:
        result = stages.build("stage1", LO00_CAL.read_bytes(), STAGE1)
        self.assertEqual(result.status, "BUILT", _codes(result))
        assert result.result is not None
        report = ck.verify_bytes(result.result.image)
        self.assertTrue(report.ok)
        self.assertEqual(report.family, "4570LO00S")


class TestExamplesAsIs(unittest.TestCase):
    """Задание 2 ревью: ВСЕ примеры заявок собираются «как есть».

    Примеры с логами (варианты 2 и 3) несут доказательства, выданные
    анализатором (``examples/logs/evidence_*.json``); запуск из каталога
    примеров, чтобы относительные пути к доказательствам сошлись.
    """

    def test_every_example_builds_as_is(self) -> None:
        old_cwd = os.getcwd()
        os.chdir(EXAMPLES)
        try:
            names = sorted(path.name for path in EXAMPLES.glob("*.example.json"))
            self.assertGreaterEqual(len(names), 9)
            for name in names:
                request = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
                image = (BN00_CAL if "bn00s" in name else BO00_CAL).read_bytes()
                result = stages.build("stage1", image, request)
                if name == "stage1_request_empty.example.json":
                    self.assertEqual("REJECTED", result.status, name)
                    continue
                self.assertIn(result.status, {"BUILT", "REQUIRES_DATA"},
                              (name, _codes(result)))
        finally:
            os.chdir(old_cwd)

    def test_variant_v2_v3_examples_write_expected_packages(self) -> None:
        # Образ 4570LO00S: единственный с доказанными воротами обогащения
        # полной нагрузки (см. веб-анализ §2), поэтому полные варианты
        # собираются целиком именно на нём.
        old_cwd = os.getcwd()
        os.chdir(EXAMPLES)
        try:
            image = LO00_CAL.read_bytes()
            request = json.loads(
                (EXAMPLES / "stage1_variant_v2.example.json").read_text(encoding="utf-8"))
            result = stages.build("stage1", image, request)
            self.assertEqual("BUILT", result.status, _codes(result))
            self.assertEqual({"ignition", "fuel", "vanos", "limits"},
                             {p.id for p in result.written_packages})
            request = json.loads(
                (EXAMPLES / "stage1_variant_v3.example.json").read_text(encoding="utf-8"))
            result = stages.build("stage1", image, request)
            self.assertEqual("BUILT", result.status, _codes(result))
            written = {p.id for p in result.written_packages}
            for expected in ("ignition", "fuel", "vanos", "limits",
                             "pops", "cat_delete"):
                self.assertIn(expected, written, expected)
        finally:
            os.chdir(old_cwd)


class TestLaunchControlAndHardware(unittest.TestCase):
    """То, что не выпускается: launch control и Stage 2 без железа."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.image = BO00_CAL.read_bytes()

    def test_launch_control_only_gives_requires_data(self) -> None:
        result = stages.build("stage1", self.image, {"intents": [{"kind": "launch_control"}]})
        self.assertEqual(result.status, "REQUIRES_DATA")
        self.assertEqual([p.id for p in result.pending], ["launch_control"])
        self.assertIsNone(result.result)
        self.assertTrue(any("калибровочный путь" in need for need in result.pending[0].needs))

    def test_launch_control_does_not_block_other_packages(self) -> None:
        request = json.loads(json.dumps(STAGE1))
        request["intents"].append({"kind": "launch_control"})
        result = stages.build("stage1", self.image, request)
        self.assertEqual(result.status, "BUILT", _codes(result))
        self.assertEqual([p.id for p in result.pending], ["launch_control"])
        self.assertEqual({p.id for p in result.written_packages}, {"limits", "pops"})

    def test_stage2_without_hardware_rejected(self) -> None:
        request = {"intents": [{"kind": "stage2"}]}
        result = stages.build("stage2", self.image, request)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("HARDWARE_REQUIRED", _codes(result))

    def test_stage2_with_hardware_builds_limits_only(self) -> None:
        result = stages.build("stage2", self.image, STAGE2)
        self.assertEqual(result.status, "BUILT", _codes(result))
        self.assertEqual({p.id for p in result.written_packages}, {"limits"})
        self.assertEqual([p.id for p in result.pending], ["hardware"])
        self.assertTrue(result.pending[0].needs)

    def test_stage_unknown(self) -> None:
        with self.assertRaises(Exception):
            stages.build("stage9", self.image, STAGE1)

    def test_bad_transmission_reported_once(self) -> None:
        """Ошибка в намерении отсечки не повторяется по пакетам."""
        request = {"intents": [{"kind": "rev_limit", "transmission": "xyz", "value": 7000}]}
        result = stages.build("stage1", self.image, request)
        self.assertEqual(result.status, "REJECTED")
        self.assertEqual(_codes(result).count("TRANSMISSION_INVALID"), 1)
        self.assertEqual(
            [item.status for item in result.packages if item.id == "limits"], ["rejected"]
        )


class TestNote(unittest.TestCase):
    """Наряд: что записано, что не менялось, что требует данных."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = stages.build("stage1", BO00_CAL.read_bytes(), STAGE1)
        cls.note = stages.build_note(cls.result, BO00_CAL.name)

    def test_sections(self) -> None:
        for header in (
            "# Наряд MS45 — STAGE1",
            "## Что записано",
            "## Что НЕ менялось",
            "## Что требует данных",
            "## Предупреждения",
            "## Порядок прошивки",
        ):
            self.assertIn(header, self.note)

    def test_no_power_promises(self) -> None:
        """Никаких «+X л.с.»: без стенда обещаний нет."""
        for pattern in (r"\+\s*\d+\s*(л\.?\s*с|hp|кВт)", r"\d+\s*(л\.?\s*с|hp)\b", r"прирост\s+\d+"):
            self.assertIsNone(re.search(pattern, self.note, re.IGNORECASE), pattern)
        self.assertIn("Мощность не обещается", self.note)

    def test_lists_written_cells(self) -> None:
        self.assertIn("0x018DC", self.note)
        self.assertIn("0x060A2", self.note)
        self.assertIn("c_t_min_pu", self.note)

    def test_mentions_signature_and_checksums(self) -> None:
        self.assertIn("RSA-подпись", self.note)
        self.assertIn("CRC-32 @0x00100", self.note)
        self.assertIn("0x40000..0x5CFFF", self.note)

    def test_pending_section_for_launch_control(self) -> None:
        request = json.loads(json.dumps(STAGE1))
        request["intents"].append({"kind": "launch_control"})
        result = stages.build("stage1", BO00_CAL.read_bytes(), request)
        note = stages.build_note(result, BO00_CAL.name)
        self.assertIn("Launch control", note)
        self.assertIn("калибровочный путь", note)

    def test_manifest_serialisable(self) -> None:
        text = json.dumps(self.result.manifest, ensure_ascii=False)
        data = json.loads(text)
        self.assertEqual(data["status"], "BUILT")
        self.assertEqual(data["family"], "4560BO00S")
        self.assertIsNotNone(data["plan"])
        self.assertEqual(data["result"]["changed_cells"], 22)


class TestCli(unittest.TestCase):
    """CLI генератора."""

    def test_build_writes_file_and_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "stage1.bin"
            note = Path(tmp) / "naryad.md"
            manifest = Path(tmp) / "manifest.json"
            code = stages.main([
                "--image", str(BO00_CAL),
                "--stage", "stage1",
                "--request", str(EXAMPLES / "stage1_request.example.json"),
                "--out", str(out),
                "--note-out", str(note),
                "--manifest-out", str(manifest),
            ])
            self.assertEqual(code, 0)
            self.assertTrue(ck.verify_image(out).ok)
            self.assertIn("Наряд MS45", note.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["status"], "BUILT")

    def test_rejected_request_returns_2_and_writes_note_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.bin"
            note = Path(tmp) / "naryad.md"
            code = stages.main([
                "--image", str(BO00_CAL),
                "--stage", "stage1",
                "--request", str(EXAMPLES / "stage1_request_empty.example.json"),
                "--out", str(out),
                "--note-out", str(note),
            ])
            self.assertEqual(code, 2)
            self.assertFalse(out.exists())
            self.assertIn("Что НЕ менялось", note.read_text(encoding="utf-8"))

    def test_requires_data_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "naryad.md"
            code = stages.main([
                "--image", str(BO00_CAL),
                "--stage", "stage1",
                "--request", str(EXAMPLES / "stage1_request_launch_control.example.json"),
                "--note-out", str(note),
            ])
            self.assertEqual(code, 2)
            self.assertIn("калибровочный путь", note.read_text(encoding="utf-8"))

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.bin"
            code = stages.main([
                "--image", str(BO00_CAL),
                "--stage", "stage1",
                "--request", str(EXAMPLES / "stage1_request.example.json"),
                "--out", str(out),
                "--dry-run",
            ])
            self.assertEqual(code, 0)
            self.assertFalse(out.exists())

    def test_missing_file(self) -> None:
        self.assertEqual(stages.main(["--image", "/nonexistent.bin", "--stage", "stage1"]), 2)


class TestRequiresDataCatalog(unittest.TestCase):
    """Каталог «что нужно измерить»: нереализованные функции честно ждут данных."""

    UNIMPLEMENTED = ("pops_intensity", "idle_speed", "disa",
                     "injectors", "e85")

    def test_catalog_covers_every_unimplemented_package(self) -> None:
        # Пакеты с реализацией для покрытых семей поддерживаются билдерами;
        # каталог также описывает условный REQUIRES_DATA (pops) и запросы
        # конкретных ролей (vmax/rev_limit), которых пока нет у BM00S.
        implemented = ("limits", "ignition", "fuel", "vanos", "cat_delete",
                       "sap_delete", "dtc_driver")
        for package in stages.STAGE_PACKAGES["stage2"]:
            if package in implemented:
                self.assertNotIn(package, stages.REQUIRES_DATA_CATALOG, package)
            else:
                self.assertIn(package, stages.REQUIRES_DATA_CATALOG, package)

    def test_every_intent_maps_to_a_known_package(self) -> None:
        for kind, package in stages.INTENT_PACKAGES.items():
            self.assertIn(package, stages.STAGE_PACKAGES["stage2"], kind)

    def test_catalog_reasons_are_concrete(self) -> None:
        for package, entry in stages.REQUIRES_DATA_CATALOG.items():
            self.assertGreater(len(entry["why"]), 40, package)
            self.assertGreaterEqual(len(entry["needs"]), 2, package)
            for need in entry["needs"]:
                self.assertGreater(len(need), 15, package)

    def test_requires_data_does_not_reject_and_names_the_code(self) -> None:
        image = BO00_CAL.read_bytes()
        for kind in self.UNIMPLEMENTED:
            with self.subTest(kind=kind):
                result = stages.build("stage1", image, {"intents": [{"kind": kind}]})
                self.assertEqual(result.status, "REQUIRES_DATA", _codes(result))
                self.assertNotIn("reject", [f["severity"] for f in result.findings])
                self.assertEqual([item.id for item in result.pending], [kind])
                pending = result.pending[0]
                self.assertEqual(pending.manifest["code"], kind.upper() + "_REQUIRES_DATA")
                self.assertTrue(pending.detail)
                self.assertTrue(pending.needs)

    def test_requires_data_package_does_not_block_the_rest(self) -> None:
        request = json.loads(json.dumps(STAGE1))
        request["intents"].append({"kind": "e85"})
        result = stages.build("stage1", BO00_CAL.read_bytes(), request)
        self.assertEqual(result.status, "BUILT", _codes(result))
        self.assertEqual({item.id for item in result.written_packages}, {"limits", "pops"})
        self.assertEqual([item.id for item in result.pending], ["e85"])

    def test_manifest_lists_requires_data_block(self) -> None:
        result = stages.build(
            "stage1", BO00_CAL.read_bytes(), {"intents": [{"kind": "injectors"}]}
        )
        block = result.manifest["requires_data"]
        self.assertEqual([item["id"] for item in block], ["injectors"])
        self.assertEqual(block[0]["code"], "INJECTORS_REQUIRES_DATA")
        self.assertTrue(block[0]["why"])
        self.assertTrue(block[0]["needs"])

    def test_example_request_with_data_needed_packages(self) -> None:
        """Пример заявки «выпускается часть, остальное ждёт данных» остаётся рабочим."""
        request = json.loads(
            (EXAMPLES / "stage1_request_data_needed.example.json").read_text(encoding="utf-8")
        )
        result = stages.build("stage1", BO00_CAL.read_bytes(), request)
        self.assertEqual(result.status, "BUILT", _codes(result))
        self.assertEqual({item.id for item in result.written_packages}, {"pops"})
        self.assertEqual([item.id for item in result.pending], ["injectors", "e85"])
        note = stages.build_note(result, BO00_CAL.name)
        for marker in ("Форсунки", "E85", "артикул форсунок"):
            self.assertIn(marker, note)

    def test_note_lists_findings(self) -> None:
        """Наряд объясняет отказ клиенту, а не только возвращает код 2."""
        request = json.loads((EXAMPLES / "stage1_request_empty.example.json").read_text(encoding="utf-8"))
        result = stages.build("stage1", BO00_CAL.read_bytes(), request)
        self.assertEqual(result.status, "REJECTED")
        text = stages.build_note(result, BO00_CAL.name)
        self.assertIn("## Замечания ядра и продукта", text)
        # Хлопки в этой заявке идут без единого подтверждения: первым срабатывает
        # требование правовой справки (задание docs/TASK_VARIANT1_RU.md §2).
        self.assertIn("ACKNOWLEDGE_LEGALITY_REQUIRED", text)
        self.assertIn("| отказ |", text)

    def test_note_explains_why_for_each_pending_package(self) -> None:
        request = json.loads(json.dumps(STAGE1))
        request["intents"] += [{"kind": "e85"}, {"kind": "disa"}]
        result = stages.build("stage1", BO00_CAL.read_bytes(), request)
        note = stages.build_note(result, BO00_CAL.name)
        for title in ("Топливо E85", "Управление DISA"):
            self.assertIn(title, note)


class TestVerifyManifest(unittest.TestCase):
    """Проверка выпущенного файла по манифесту: суммы, ячейки, «ничего лишнего»."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.out = cls.dir / "stage1.bin"
        cls.manifest = cls.dir / "manifest.json"
        code = stages.main([
            "--image", str(BO00_CAL),
            "--stage", "stage1",
            "--request", str(EXAMPLES / "stage1_request.example.json"),
            "--out", str(cls.out),
            "--manifest-out", str(cls.manifest),
        ])
        assert code == 0, code

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_verified_file(self) -> None:
        verification = stages.verify_manifest(self.manifest)
        self.assertEqual(verification.status, "VERIFIED", verification.as_dict())
        self.assertTrue(verification.ok)
        self.assertEqual(verification.image_path, str(self.out))
        self.assertEqual(verification.source_path, str(BO00_CAL))
        names = {item.name: item.ok for item in verification.checks}
        for name in ("manifest_version", "sha256", "checksums", "family", "planned_cells",
                     "changed_bytes", "outside_plan_changes", "cal_region_only"):
            self.assertTrue(names[name], (name, verification.as_dict()))

    def test_markdown_report(self) -> None:
        text = stages.verification_as_markdown(stages.verify_manifest(self.manifest))
        self.assertIn("# Проверка файла по манифесту", text)
        self.assertIn("| sha256 | OK |", text)
        self.assertIn("соответствует манифесту", text)

    def test_tampered_cell_is_caught(self) -> None:
        data = bytearray(self.out.read_bytes())
        data[0x018DC] ^= 0x01  # запланированная ячейка c_vs_max
        tampered = self.dir / "tampered_cell.bin"
        tampered.write_bytes(bytes(data))
        verification = stages.verify_manifest(self.manifest, tampered)
        self.assertEqual(verification.status, "FAILED")
        names = {item.name: item.ok for item in verification.checks}
        self.assertFalse(names["sha256"])
        self.assertFalse(names["planned_cells"])

    def test_tampered_region_outside_plan_is_caught(self) -> None:
        data = bytearray(self.out.read_bytes())
        data[0x08000] ^= 0xFF  # внутри калибровки, но вне плана
        tampered = self.dir / "tampered_region.bin"
        tampered.write_bytes(bytes(data))
        verification = stages.verify_manifest(self.manifest, tampered)
        self.assertEqual(verification.status, "FAILED")
        failed = {item.name for item in verification.checks if not item.ok}
        self.assertIn("outside_plan_changes", failed)

    def test_other_file_is_not_accepted(self) -> None:
        verification = stages.verify_manifest(self.manifest, LO00_CAL)
        self.assertEqual(verification.status, "FAILED")
        self.assertIn("sha256", {item.name for item in verification.checks if not item.ok})

    def test_manifest_without_result_name_needs_explicit_file(self) -> None:
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        data.pop("result_name", None)
        path = self.dir / "no_result_name.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        verification = stages.verify_manifest(path)
        self.assertEqual(verification.status, "FAILED")
        self.assertIn("result_name", {item.name for item in verification.checks})

    def test_not_built_manifest(self) -> None:
        manifest = self.dir / "requires_data.json"
        code = stages.main([
            "--image", str(BO00_CAL),
            "--stage", "stage1",
            "--request", str(EXAMPLES / "stage1_request_launch_control.example.json"),
            "--manifest-out", str(manifest),
        ])
        self.assertEqual(code, 2)
        verification = stages.verify_manifest(manifest)
        self.assertEqual(verification.status, "NOT_BUILT")
        self.assertFalse(verification.ok)
        # CLI не считает отсутствие файла ошибкой проверки.
        self.assertEqual(stages.main(["--verify-manifest", str(manifest)]), 0)

    def test_cli_verify_exit_codes(self) -> None:
        report = self.dir / "verify.md"
        self.assertEqual(
            stages.main(["--verify-manifest", str(self.manifest), "--note-out", str(report)]), 0
        )
        self.assertIn("Проверка файла по манифесту", report.read_text(encoding="utf-8"))
        data = bytearray(self.out.read_bytes())
        data[0x018DC] ^= 0x01
        tampered = self.dir / "bad.bin"
        tampered.write_bytes(bytes(data))
        self.assertEqual(
            stages.main(["--verify-manifest", str(self.manifest), "--image", str(tampered)]), 2
        )


class TestFullImageVerify(unittest.TestCase):
    """Полный образ 1 МиБ: проверка по манифесту видит границы калибровки."""

    BN_FULL = MS45 / "firmware_dumps" / "MS45.0_0044560BN_fullread_ORI.bin"

    def test_full_image_verified_and_confined_to_cal_region(self) -> None:
        if not self.BN_FULL.exists():  # pragma: no cover - зависит от набора дампов
            self.skipTest("нет полного дампа")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "full.bin"
            manifest = Path(tmp) / "manifest.json"
            code = stages.main([
                "--image", str(self.BN_FULL),
                "--stage", "stage1",
                "--request", str(EXAMPLES / "stage1_request_bn00s.example.json"),
                "--out", str(out),
                "--manifest-out", str(manifest),
            ])
            self.assertEqual(code, 0)
            verification = stages.verify_manifest(manifest, out)
            self.assertEqual(verification.status, "VERIFIED", verification.as_dict())
            names = {item.name: item for item in verification.checks}
            self.assertTrue(names["cal_region_only"].ok)
            self.assertIn("внутри калибровки", names["cal_region_only"].detail)
            # Файл остался полным образом: калибровочная область — на своём месте.
            self.assertEqual(len(out.read_bytes()), len(self.BN_FULL.read_bytes()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

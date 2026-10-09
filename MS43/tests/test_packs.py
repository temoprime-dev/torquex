"""Пакеты «зажигание / топливо / VANOS» (задание docs/TASK_PACKS_RU.md).

Проверяет ворота из веб-анализа (`docs/PACKS_WEB_ANALYSIS_RU.md`) и
машиночитаемой спецификации (`docs/pack_log_requirements.json`):

* зажигание: добавка к карте RON98; Stage 1 без лога ≤ +3.0° для профиля
  RON98 (≤ +1.5° для RON95, RON91 — без добавки), Stage 2 с логом ≤ +5.0°;
  MBT ``ip_iga_optm_tco_2`` поднимается связно по той же добавке, карта RON91
  не изменяется никогда
  (задание docs/TASK_STAGE1_RU.md §2, потолки — docs/pack_log_requirements.json);
* топливо: малая добавка ``ip_tib`` без лога; обогащение ``ip_ti_fl__n``
  только с логом широкой лямбды, λ не ниже 0.82 и не обедняем;
* ВАНос: только тёплые карты частичной нагрузки и только по логу фаз;
* защищённые символы (пороги полной нагрузки, модель момента) не трогаются.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MS43.tools import checksum  # noqa: E402
from MS43.tools import stages  # noqa: E402
from MS43.tools.policy import load_policy  # noqa: E402
from tools import pack_log_analyzer as analyzer  # noqa: E402
from tools import pack_log_fixtures as fx  # noqa: E402
from tools import variant1  # noqa: E402

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware"
ARCHIVE = Path(__file__).resolve().parents[1] / "firmware_archive"
REFERENCE = FIRMWARE / "Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin"
SPEC = json.loads(
    (Path(__file__).resolve().parents[2] / "docs" / "pack_log_requirements.json")
    .read_text(encoding="utf-8")
)

# Задание 1 ревью: доказательства создаёт только анализатор. Фикстуры —
# синтетические логи (реальные придут от клиентов); они пишутся во временный
# каталог один раз на сессию тестов и прогоняются через анализатор.
import tempfile  # noqa: E402

FIXTURE_DIR = Path(tempfile.mkdtemp(prefix="ms43_pack_log_"))
GOOD_IGNITION = fx.evidence_from_csv(
    fx.write_ignition_csv(FIXTURE_DIR / "ignition_good.csv"), "ignition")
GOOD_FUEL = fx.evidence_from_csv(
    fx.write_fuel_csv(FIXTURE_DIR / "fuel_good.csv"), "fuel")
GOOD_VANOS = fx.evidence_from_csv(
    fx.write_vanos_csv(FIXTURE_DIR / "vanos_good.csv"), "vanos")


def evidence(package: str, **results) -> dict:
    """Ручное доказательство — только для проверки порогового слоя
    ``validate_pack_evidence``; ворота пакетов такое не принимают."""
    return {
        "kind": "pack_log",
        "schema_version": 1,
        "package": package,
        "source": "тестовый лог (фикстура)",
        "fuel_ron": 98,
        "results": results,
    }


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.image = REFERENCE.read_bytes()
        cls.policy = load_policy()

    def build(self, intents: list[dict], **extra) -> stages.StageResult:
        request = {"stage": "stage1", "customer": "тест", "intents": intents, **extra}
        # Правовая справка (задание docs/TASK_VARIANT1_RU.md §2) — предмет
        # отдельного теста; здесь подтверждение ставится, чтобы проверять сам
        # пакет/пакет-слой, а не подтверждение.
        if any(item.get("kind") in variant1.LEGAL_GATE_KINDS for item in intents):
            request.setdefault("acknowledge_legality", True)
        return stages.build("stage1", self.image, request, policy=self.policy)

    def cells(self, image: bytes, symbol: str) -> list[float]:
        entry = self.policy.objects[symbol]
        return [stages._current(image, entry, i) for i in range(entry.rows * entry.columns)]

    def codes(self, result) -> list[str]:
        return [item["code"] for item in result.findings]

    def verify(self, image: bytes) -> None:
        """CRC-16 выпущенного образа сходится (ядро пересчитало суммы)."""
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.bin"
            path.write_bytes(image)
            self.assertTrue(checksum.verify_image(path).ok)


class EvidenceValidationTests(Base):
    def test_spec_file_is_loaded_and_pinned(self) -> None:
        ign = SPEC["packages"]["ignition"]
        self.assertEqual(3.0, ign["gates"]["delta_without_log_deg"])
        self.assertEqual(5.0, ign["gates"]["delta_with_log_deg"])
        self.assertEqual(3.0, ign["gates"]["octane_no_log_deg"]["ron98"])
        self.assertEqual(1.5, ign["gates"]["octane_no_log_deg"]["ron95"])
        self.assertEqual(0.0, ign["gates"]["octane_no_log_deg"]["ron91"])
        layout = ign["layout"]
        self.assertEqual(1.5, layout["neighbour_max_step_deg"])
        self.assertEqual(0.375, layout["quantum_deg"])
        self.assertEqual(0.99, ign["no_knock"]["min_fraction_above_load"])
        self.assertEqual(98, SPEC["conditions"]["min_ron_for_ignition_evidence"])

    def test_missing_evidence(self) -> None:
        ok, problems = stages.validate_pack_evidence(None, "ignition")
        self.assertFalse(ok)
        self.assertTrue(problems)

    def test_wrong_kind_or_package(self) -> None:
        bad = dict(GOOD_IGNITION, kind="stub")
        ok, _ = stages.validate_pack_evidence(bad, "ignition")
        self.assertFalse(ok)
        bad = dict(GOOD_IGNITION, package="fuel")
        ok, problems = stages.validate_pack_evidence(bad, "ignition")
        self.assertFalse(ok)
        self.assertTrue(any("fuel" in item for item in problems))

    def test_source_is_required(self) -> None:
        bad = dict(GOOD_IGNITION, source="  ")
        ok, problems = stages.validate_pack_evidence(bad, "ignition")
        self.assertFalse(ok)
        self.assertTrue(any("source" in item for item in problems))

    def test_each_ignition_threshold_is_checked(self) -> None:
        cases = [
            dict(GOOD_IGNITION, fuel_ron=92),
            dict(GOOD_IGNITION, results={"no_knock_fraction": 0.9,
                                         "max_sustained_retard_seconds": 0.1,
                                         "knock_spike_events": 0, "max_iat_c": 30.0}),
            dict(GOOD_IGNITION, results={"no_knock_fraction": 0.999,
                                         "max_sustained_retard_seconds": 0.9,
                                         "knock_spike_events": 0, "max_iat_c": 30.0}),
            dict(GOOD_IGNITION, results={"no_knock_fraction": 0.999,
                                         "max_sustained_retard_seconds": 0.1,
                                         "knock_spike_events": 2, "max_iat_c": 30.0}),
            dict(GOOD_IGNITION, results={"no_knock_fraction": 0.999,
                                         "max_sustained_retard_seconds": 0.1,
                                         "knock_spike_events": 0, "max_iat_c": 60.0}),
        ]
        for bad in cases:
            ok, problems = stages.validate_pack_evidence(bad, "ignition")
            self.assertFalse(ok, bad)
            self.assertTrue(problems)
        ok, problems = stages.validate_pack_evidence(GOOD_IGNITION, "ignition")
        self.assertTrue(ok, problems)


class AnalyzerGateTests(Base):
    """Задание 1 ревью: ворота принимают только доказательство анализатора."""

    def test_manual_json_is_rejected_with_own_code(self) -> None:
        manual = evidence(
            "ignition", no_knock_fraction=0.999,
            max_sustained_retard_seconds=0.1,
            knock_spike_events=0, max_iat_c=30.0,
        )
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 2.5,
                              "evidence": manual}])
        self.assertEqual("REQUIRES_DATA", result.status)
        package = next(p for p in result.packages if p.id == "ignition")
        self.assertIn(analyzer.EVIDENCE_MANUAL_JSON, package.detail)

    def test_tampered_results_are_rejected(self) -> None:
        tampered = dict(GOOD_IGNITION)
        tampered["results"] = dict(GOOD_IGNITION["results"],
                                   no_knock_fraction=0.9)
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 2.5,
                              "evidence": tampered}])
        self.assertEqual("REQUIRES_DATA", result.status)
        package = next(p for p in result.packages if p.id == "ignition")
        self.assertIn(analyzer.EVIDENCE_TAMPERED, package.detail)

    def test_missing_source_log_is_rejected(self) -> None:
        lost = dict(GOOD_IGNITION, source="нет/такого/файла.csv")
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 2.5,
                              "evidence": lost}])
        self.assertEqual("REQUIRES_DATA", result.status)
        package = next(p for p in result.packages if p.id == "ignition")
        self.assertIn(analyzer.EVIDENCE_SOURCE_NOT_FOUND, package.detail)

    def test_changed_log_is_rejected_by_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            csv_path = fx.write_ignition_csv(Path(directory) / "wot.csv")
            proof = fx.evidence_from_csv(csv_path, "ignition")
            # лог «подправили» после выдачи доказательства (колонка угла)
            text = csv_path.read_text(encoding="utf-8").split("\n")
            cells = text[100].split(";")
            cells[4] = f"{float(cells[4]) + 7.0:.2f}"
            text[100] = ";".join(cells)
            csv_path.write_text("\n".join(text), encoding="utf-8")
            ok, problems = analyzer.verify_evidence(proof, "ignition")
        self.assertFalse(ok)
        self.assertTrue(any(analyzer.EVIDENCE_SHA256_MISMATCH in p
                            for p in problems), problems)

    def test_analyzer_evidence_passes_the_gate(self) -> None:
        ok, problems = analyzer.verify_evidence(GOOD_IGNITION, "ignition")
        self.assertTrue(ok, problems)

    def test_lf_and_crlf_log_give_the_same_evidence(self) -> None:
        """Задание 1 ревью №2: клиентские логи и checkout на Windows бывают с
        CRLF — sha256 считается по нормализованному содержимому, поэтому один
        и тот же лог даёт одно доказательство на любой платформе."""
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            lf = fx.write_ignition_csv(directory / "wot_lf.csv")
            crlf = directory / "wot_crlf.csv"
            crlf.write_bytes(lf.read_bytes().replace(b"\n", b"\r\n"))
            proof_lf = analyzer.analyze_pack_log(lf, "ignition", 98)
            proof_crlf = analyzer.analyze_pack_log(crlf, "ignition", 98)
            self.assertEqual(proof_lf["source_sha256"], proof_crlf["source_sha256"])
            self.assertEqual(proof_lf["results"], proof_crlf["results"])
            self.assertEqual(analyzer.SHA256_NORMALIZATION,
                             proof_crlf["sha256_normalization"])
            # доказательство, выданное по LF-файлу, проходит по CRLF-копии
            cross = dict(proof_lf, source=str(crlf))
            ok, problems = analyzer.verify_evidence(cross, "ignition")
            self.assertTrue(ok, problems)

    def test_example_evidence_survives_a_crlf_checkout(self) -> None:
        """Сценарий ревью: репозиторий склонирован на Windows с autocrlf=true,
        CSV примеров стали CRLF — пакеты примеров обязаны остаться зелёными."""
        logs = Path(__file__).resolve().parents[1] / "examples" / "logs"
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            crlf_csv = directory / "wot_knock_free.csv"
            crlf_csv.write_bytes(
                (logs / "wot_knock_free.csv").read_bytes().replace(b"\n", b"\r\n"))
            proof = json.loads(
                (logs / "evidence_ignition.json").read_text(encoding="utf-8"))
            proof["source"] = str(crlf_csv)
            ok, problems = analyzer.verify_evidence(proof, "ignition")
            self.assertTrue(ok, problems)
            result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 2.5,
                                  "evidence": proof}])
            self.assertEqual("BUILT", result.status)


class AnalyzerBoundaryTests(unittest.TestCase):
    """Синтетические фикстуры на граничные случаи (задание 1 ревью)."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dir = Path(self.directory.name)

    def test_knock_retard_invalidates_the_log(self) -> None:
        csv_path = fx.write_ignition_csv(self.dir / "knock.csv", knock=True)
        outcome = analyzer.analyze_pack_log(csv_path, "ignition", 98)
        self.assertFalse(outcome["valid"])
        self.assertGreater(outcome["results"]["max_sustained_retard_seconds"],
                           SPEC["packages"]["ignition"]["no_knock"]["max_sustained_seconds"])
        ok, _ = analyzer.verify_evidence(outcome, "ignition")
        self.assertTrue(ok)  # ворота целостности проходят; лог не годен по данным
        self.assertTrue(any("детонаци" in issue or "коррекц" in issue
                            for issue in outcome["issues"]), outcome["issues"])

    def test_hot_intake_invalidates_the_log(self) -> None:
        csv_path = fx.write_ignition_csv(self.dir / "hot.csv", hot_iat=True)
        outcome = analyzer.analyze_pack_log(csv_path, "ignition", 98)
        self.assertFalse(outcome["valid"])
        self.assertGreater(outcome["results"]["max_iat_c"],
                           SPEC["conditions"]["intake_temp_max_c"])

    def test_low_ron_invalidates_ignition_evidence(self) -> None:
        csv_path = fx.write_ignition_csv(self.dir / "ron92.csv")
        outcome = analyzer.analyze_pack_log(csv_path, "ignition", 92)
        self.assertFalse(outcome["valid"])
        self.assertTrue(any("RON" in issue for issue in outcome["issues"]))

    def test_non_monotonic_time_rejects_the_whole_log(self) -> None:
        csv_path = fx.write_ignition_csv(self.dir / "t.csv")
        lines = csv_path.read_text(encoding="utf-8").split("\n")
        head, rest = lines[0], lines[1:]
        rest[50] = "0.00" + rest[50][rest[50].index(";"):]
        csv_path.write_text("\n".join([head] + rest), encoding="utf-8")
        outcome = analyzer.analyze_pack_log(csv_path, "ignition", 98)
        self.assertFalse(outcome["valid"])
        self.assertTrue(any("монотонно" in issue for issue in outcome["issues"]))

    def test_unstable_wot_lambda_invalidates_fuel(self) -> None:
        csv_path = fx.write_fuel_csv(self.dir / "unstable.csv", unstable=True)
        outcome = analyzer.analyze_pack_log(csv_path, "fuel")
        self.assertFalse(outcome["valid"])
        self.assertGreater(outcome["results"]["lambda_stdev_wot"],
                           SPEC["packages"]["fuel"]["criteria"]["lambda_stdev_max_wot"])

    def test_lean_wot_lambda_invalidates_fuel(self) -> None:
        csv_path = fx.write_fuel_csv(self.dir / "lean.csv", lean=True)
        outcome = analyzer.analyze_pack_log(csv_path, "fuel")
        self.assertFalse(outcome["valid"])
        self.assertLess(outcome["results"]["lambda_floor"],
                        SPEC["packages"]["fuel"]["criteria"]["lambda_target_min"])

    def test_narrowband_sensor_is_not_evidence(self) -> None:
        csv_path = fx.write_fuel_csv(self.dir / "nb.csv", narrowband=True)
        outcome = analyzer.analyze_pack_log(csv_path, "fuel")
        self.assertFalse(outcome["valid"])
        self.assertLess(outcome["results"]["lambda_variance"],
                        SPEC["packages"]["fuel"]["criteria"]["wideband_variance_min"])

    def test_big_partial_load_trims_invalidate_fuel(self) -> None:
        csv_path = fx.write_fuel_csv(self.dir / "trim.csv", big_trim=True)
        outcome = analyzer.analyze_pack_log(csv_path, "fuel")
        self.assertFalse(outcome["valid"])
        self.assertGreater(
            outcome["results"]["partial_load_trim_max_percent"],
            SPEC["packages"]["fuel"]["criteria"]["partial_load_trim_max_percent"])

    def test_vanos_track_error_invalidates_the_grid(self) -> None:
        csv_path = fx.write_vanos_csv(self.dir / "track.csv", track_error_deg=5.0)
        outcome = analyzer.analyze_pack_log(csv_path, "vanos")
        self.assertFalse(outcome["valid"])
        self.assertGreater(outcome["results"]["max_track_error_deg"],
                           SPEC["packages"]["vanos"]["criteria"]["max_track_error_deg"])

    def test_incomplete_vanos_grid_invalidates_the_log(self) -> None:
        csv_path = fx.write_vanos_csv(self.dir / "short.csv", drop_points=6)
        outcome = analyzer.analyze_pack_log(csv_path, "vanos")
        self.assertFalse(outcome["valid"])
        self.assertLess(outcome["results"]["hold_points"],
                        outcome["results"]["hold_points_required"])

    def test_invalid_ignition_log_keeps_add_at_variant_one(self) -> None:
        csv_path = fx.write_ignition_csv(self.dir / "knock.csv", knock=True)
        proof = fx.evidence_from_csv(csv_path, "ignition")
        request = {"stage": "stage1", "customer": "тест",
                   "intents": [{"id": "i1", "kind": "ignition", "delta_deg": 2.5,
                                "evidence": proof}]}
        result = stages.build("stage1", REFERENCE.read_bytes(), request,
                              policy=load_policy())
        self.assertEqual("REQUIRES_DATA", result.status)


class RealLoggerFormatTests(unittest.TestCase):
    """Задание 2 ревью №2: реальный лог клиента (заголовки с единицами в
    квадратных скобках, номера цилиндров в круглых) обязан разбираться;
    недостающие каналы сообщаются точным списком с подсказками."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dir = Path(self.directory.name)

    def test_all_available_channels_are_recognized(self) -> None:
        csv_path = fx.write_e46_logger_csv(self.dir / "full.csv", full=True)
        table = analyzer.read_pack_csv(csv_path)
        channels = table["channels"]
        for name in ("time", "rpm", "load", "ignition", "maf_airflow",
                     "intake_temp", "vehicle_speed", "injector_pulse_width",
                     "lambda_wideband", "vanos_intake_actual",
                     "vanos_exhaust_actual", "coolant_temp", "knock_retard"):
            self.assertTrue(any(v is not None for v in channels[name]), name)
        for cylinder in range(1, 7):
            self.assertTrue(
                any(v is not None for v in channels[f"knock_cyl_{cylinder}"]),
                cylinder)

    def test_missing_channels_report_is_exact(self) -> None:
        # Реальный лог из ревью: нет ОЖ, педали и коррекции детонации.
        # Педаль — заменима (по нагрузке), поэтому в списке только два канала.
        csv_path = fx.write_e46_logger_csv(self.dir / "real.csv")
        outcome = analyzer.analyze_pack_log(csv_path, "ignition", 98)
        self.assertFalse(outcome["valid"])
        report = next(issue for issue in outcome["issues"]
                      if issue.startswith("нет обязательных каналов"))
        self.assertIn("coolant_temp", report)
        self.assertIn("knock_retard", report)
        self.assertNotIn("pedal", report)
        self.assertNotIn("load —", report)
        self.assertIn("включите в логгере", report)

    def test_log_without_pedal_is_analyzed_by_load(self) -> None:
        # В формате реального логгера педали нет: «газ в пол» находится по
        # нагрузке, и дополненный лог проходит ворота целиком.
        csv_path = fx.write_e46_logger_csv(self.dir / "full.csv", full=True)
        outcome = analyzer.analyze_pack_log(csv_path, "ignition", 98)
        self.assertTrue(outcome["valid"], outcome["issues"])
        self.assertEqual("load", outcome["results"]["wot_detection"])
        self.assertEqual(3, outcome["results"]["wot_runs"])

    def test_knock_voltage_is_an_optional_channel(self) -> None:
        csv_path = fx.write_e46_logger_csv(self.dir / "nokv.csv", full=True,
                                           knock_voltage=False)
        outcome = analyzer.analyze_pack_log(csv_path, "ignition", 98)
        self.assertTrue(outcome["valid"], outcome["issues"])
        self.assertEqual(0, outcome["results"]["knock_spike_events"])
        self.assertTrue(any("вспомогательный" in note
                            for note in outcome["notes"]))

    def test_unit_mismatch_skips_the_channel(self) -> None:
        csv_path = fx.write_e46_logger_csv(self.dir / "full.csv", full=True)
        text = csv_path.read_text(encoding="utf-8").replace(
            "Engine Speed [RPM]", "Engine Speed [km/h]", 1)
        csv_path.write_text(text, encoding="utf-8")
        outcome = analyzer.analyze_pack_log(csv_path, "ignition", 98)
        self.assertFalse(outcome["valid"])
        joined = "; ".join(outcome["issues"])
        self.assertIn("rpm", joined)
        self.assertIn("не совпадает", joined)


class IgnitionTests(Base):
    def test_small_add_without_log_is_written(self) -> None:
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 1.5}])
        self.assertEqual("BUILT", result.status)
        before = self.cells(self.image, "ip_iga_ron_98_pl_ivvt__n__maf")
        after = self.cells(result.result.image, "ip_iga_ron_98_pl_ivvt__n__maf")
        changed = [(b, a) for b, a in zip(before, after) if a != b]
        self.assertTrue(changed)
        for b, a in changed:
            # квант сетки 0.375°: добавка прижата к сетке и не больше 1.5° + полкванта
            self.assertGreater(a, b)
            self.assertLessEqual(a - b, 1.5 + 0.375 / 2 + 1e-9)
            self.assertAlmostEqual((a - b) % 0.375, 0.0, places=6)

    def test_ron91_map_is_never_touched(self) -> None:
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 1.5}])
        self.assertEqual("BUILT", result.status)
        entry = self.policy.objects["ip_iga_ron_91_pl_ivvt__n__maf"]
        region = range(entry.address, entry.address + entry.rows * entry.columns)
        for offset in region:
            self.assertEqual(self.image[offset], result.result.image[offset],
                             f"карта RON91 изменена по смещению {offset:#x}")

    def test_linked_optm_is_raised_with_ron98(self) -> None:
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 1.5}])
        self.assertEqual("BUILT", result.status, result.findings)
        ron_before = self.cells(self.image, "ip_iga_ron_98_pl_ivvt__n__maf")
        ron_after = self.cells(result.result.image, "ip_iga_ron_98_pl_ivvt__n__maf")
        mbt_before = self.cells(self.image, "ip_iga_optm_tco_2__n__maf")
        mbt_after = self.cells(result.result.image, "ip_iga_optm_tco_2__n__maf")
        ron_add = [after - before for before, after in zip(ron_before, ron_after)]
        mbt_add = [after - before for before, after in zip(mbt_before, mbt_after)]
        self.assertTrue(any(value > 0 for value in ron_add))
        self.assertTrue(any(value > 0 for value in mbt_add))
        self.assertIn("ip_iga_optm_tco_2__n__maf",
                      {item.symbol for item in result.plan.symbols})
        self.assertLessEqual(max(ron_add), 1.5 + 0.375 / 2 + 1e-9)
        self.assertLessEqual(max(mbt_add), max(ron_add) + 1e-9)

    def test_big_add_requires_log(self) -> None:
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 2.5}])
        self.assertEqual("REQUIRES_DATA", result.status)
        package = next(p for p in result.packages if p.id == "ignition")
        self.assertEqual("requires_data", package.status)
        self.assertTrue(package.needs)

    def test_big_add_with_log_is_written_and_capped(self) -> None:
        result = self.build(
            [{"id": "i1", "kind": "ignition", "delta_deg": 2.5, "evidence": GOOD_IGNITION}]
        )
        self.assertEqual("BUILT", result.status)
        before = self.cells(self.image, "ip_iga_ron_98_pl_ivvt__n__maf")
        after = self.cells(result.result.image, "ip_iga_ron_98_pl_ivvt__n__maf")
        changed = [(b, a) for b, a in zip(before, after) if a != b]
        self.assertTrue(changed)
        for b, a in changed:
            self.assertLessEqual(a - b, 2.5 + 0.375 / 2 + 1e-9)

    def test_bad_fuel_evidence_rejects_the_big_add(self) -> None:
        bad = dict(GOOD_IGNITION, fuel_ron=92)
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 2.5,
                              "evidence": bad}])
        self.assertEqual("REQUIRES_DATA", result.status)
        package = next(p for p in result.packages if p.id == "ignition")
        self.assertIn("RON 98", package.detail)

    def test_evidence_file_path_is_accepted(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.json"
            path.write_text(json.dumps(GOOD_IGNITION, ensure_ascii=False), encoding="utf-8")
            result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 2.5,
                                  "evidence": str(path)}])
        self.assertEqual("BUILT", result.status)

    def test_delta_bounds(self) -> None:
        for bad in (0, -1, 5.01):
            result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": bad}])
            self.assertEqual("REJECTED", result.status, bad)
            codes = {f["code"] for f in result.findings}
            self.assertIn("REQUEST_INVALID", codes)
        # 3.5 — не отказ: без логов это «принеси лог детонации», с логом — проходит
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 3.5}])
        self.assertEqual("REQUIRES_DATA", result.status)


class Stage1CeilingTests(Base):
    """Границы Stage 1/Stage 2 из единой спецификации (§2.1 задания).

    Точные грани: 3,0° записывается без логов (профиль RON98), 3,01° — нет;
    с логом детонации 5,0° записывается, 5,01° — отказ. Для RON95 граница
    Stage 1 — 1,5°. Потолки читаются из docs/pack_log_requirements.json
    (tools/ignition_caps.py), здесь проверяется ровно поведение на границе.
    """

    def deltas(self, result) -> list[float]:
        before = self.cells(self.image, "ip_iga_ron_98_pl_ivvt__n__maf")
        after = self.cells(result.result.image, "ip_iga_ron_98_pl_ivvt__n__maf")
        return [a - b for b, a in zip(before, after)]

    def test_ron98_three_degrees_without_log_is_written(self) -> None:
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 3.0}],
                            octane="ron98")
        self.assertEqual("BUILT", result.status, result.findings)
        deltas = self.deltas(result)
        self.assertTrue(any(d > 0 for d in deltas))
        self.assertLessEqual(max(deltas), 3.0 + 1e-9)

    def test_ron98_above_three_needs_the_log(self) -> None:
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 3.01}],
                            octane="ron98")
        self.assertEqual("REQUIRES_DATA", result.status, result.findings)
        package = next(p for p in result.packages if p.id == "ignition")
        self.assertIn("требует лога детонации", package.detail)

    def test_five_degrees_with_log_is_written(self) -> None:
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 5.0,
                              "evidence": GOOD_IGNITION}], octane="ron98")
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertLessEqual(max(self.deltas(result)), 5.0 + 0.375 / 2 + 1e-9)

    def test_above_five_is_refused_even_with_log(self) -> None:
        for delta in (5.01, 6.0):
            result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": delta,
                                  "evidence": GOOD_IGNITION}], octane="ron98")
            self.assertEqual("REJECTED", result.status, delta)
            self.assertIn("REQUEST_INVALID", {f["code"] for f in result.findings})

    def test_ron95_boundary_is_one_and_a_half(self) -> None:
        ok = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 1.5}],
                        octane="ron95")
        self.assertEqual("BUILT", ok.status, ok.findings)
        above = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 1.51}],
                           octane="ron95")
        self.assertEqual("REQUIRES_DATA", above.status, above.findings)

    def test_no_profile_keeps_the_old_one_and_a_half_cap(self) -> None:
        """Профиль не выбран — консервативный потолок 1,5° (как в v1)."""
        ok = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 1.5}])
        self.assertEqual("BUILT", ok.status, ok.findings)
        above = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 1.6}])
        self.assertEqual("REQUIRES_DATA", above.status)

    def test_addition_is_not_flat_and_neighbour_jumps_are_bounded(self) -> None:
        """§2а: связанная добавка неплоская, соседи — не дальше шага."""
        from tools import ignition_caps, ignition_layout

        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 3.0}],
                            octane="ron98")
        self.assertEqual("BUILT", result.status, result.findings)
        deltas = self.deltas(result)
        entry = self.policy.objects["ip_iga_ron_98_pl_ivvt__n__maf"]
        jump = ignition_layout.max_neighbour_jump(deltas, entry.rows, entry.columns)
        self.assertLessEqual(jump, ignition_caps.layout()["neighbour_max_step_deg"] + 1e-9)
        positive = [d for d in deltas if d > 0]
        self.assertTrue(positive)
        # не плоская: различных значений добавки больше одного
        self.assertGreater(len({round(d, 6) for d in positive}), 1)
        # сетка 0.375°, форма не плоская, а старый MBT не обнуляет
        # добавку: его карта получает ту же раскладку отдельным linked write.
        for d in deltas:
            self.assertAlmostEqual(d % 0.375, 0.0, places=6)
            self.assertGreaterEqual(d, -1e-9)
        self.assertIn("ip_iga_optm_tco_2__n__maf",
                      {item.symbol for item in result.plan.symbols})
        rpm = stages._read_all(self.image, self.policy.objects["ldpm_n_14"])
        entry = self.policy.objects["ip_iga_ron_98_pl_ivvt__n__maf"]
        self.assertGreater(
            sum(1 for row in range(entry.rows) if rpm[row] > 3000
                for value in deltas[row * entry.columns:(row + 1) * entry.columns]
                if value > 0),
            0,
        )

    def test_stage1_variant_v1_gate_follows_profile(self) -> None:
        """Ворота v1 = потолок профиля: ron98 пропускает 3.0, ron95 — нет."""
        ok = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 3.0}],
                        variant="v1", octane="ron98")
        self.assertEqual("BUILT", ok.status, ok.findings)
        bad = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 3.0}],
                         variant="v1", octane="ron95")
        self.assertEqual("REJECTED", bad.status)
        self.assertIn("VARIANT_SCOPE_VIOLATION", {f["code"] for f in bad.findings})


class FuelTests(Base):
    def test_tib_add_without_log_is_written(self) -> None:
        result = self.build([{"id": "f1", "kind": "fuel", "tib_add_ms": 0.15,
                              "tib_add_percent": 4.0}])
        self.assertEqual("BUILT", result.status)
        before = self.cells(self.image, "ip_tib__n__maf")
        after = self.cells(result.result.image, "ip_tib__n__maf")
        changed = [(b, a) for b, a in zip(before, after) if a != b]
        self.assertTrue(changed)
        for b, a in changed:
            self.assertGreater(a, b)
            # обе границы: ≤ +0.25 мс и ≤ +4% от стока (+ половина кванта 1/187.5 мс)
            quantum = 0.0053333333 / 2
            self.assertLessEqual(a - b, min(0.25, b * 0.04) + quantum + 1e-6)

    def test_enrichment_requires_wideband_log(self) -> None:
        result = self.build([{"id": "f1", "kind": "fuel", "ti_fl_enrich_lambda": 0.03}])
        self.assertEqual("REQUIRES_DATA", result.status)
        package = next(p for p in result.packages if p.id == "fuel")
        self.assertEqual("requires_data", package.status)

    def test_enrichment_with_log_respects_lambda_floor(self) -> None:
        result = self.build([{"id": "f1", "kind": "fuel", "ti_fl_enrich_lambda": 0.03,
                              "evidence": GOOD_FUEL}])
        self.assertEqual("BUILT", result.status)
        before = self.cells(self.image, "ip_ti_fl__n")
        after = self.cells(result.result.image, "ip_ti_fl__n")
        self.assertTrue(any(a != b for b, a in zip(before, after)))
        for b, a in zip(before, after):
            self.assertGreaterEqual(a, b - 1e-9)  # никогда не обедняем
            if a != b:
                # изменённая ячейка обязана остаться не беднее λ 0.82;
                # сток, который уже богаче пола, не трогается вовсе
                self.assertGreaterEqual(1.0 - a, 0.82 - 1e-6)
            self.assertLessEqual(a - b, 0.05 + 1e-3)  # сдвиг λ не больше 0.05

    def test_empty_request_waits_for_data(self) -> None:
        result = self.build([{"id": "f1", "kind": "fuel"}])
        self.assertEqual("REQUIRES_DATA", result.status)


class VanosTests(Base):
    def test_shift_requires_phase_log(self) -> None:
        result = self.build([{"id": "v1", "kind": "vanos", "shift_in_deg": 2.0}])
        self.assertEqual("REQUIRES_DATA", result.status)
        package = next(p for p in result.packages if p.id == "vanos")
        self.assertEqual("requires_data", package.status)
        self.assertTrue(package.needs)

    def test_shift_with_log_is_written(self) -> None:
        result = self.build([{"id": "v1", "kind": "vanos", "shift_in_deg": 2.0,
                              "shift_ex_deg": -2.0, "evidence": GOOD_VANOS}])
        self.assertEqual("BUILT", result.status)
        for symbol, shift in (
            ("ip_cam_sp_tco_2_in_pl__n__maf_iv", 2.0),
            ("ip_cam_sp_tco_2_ex_pl__n__maf_iv", -2.0),
        ):
            before = self.cells(self.image, symbol)
            after = self.cells(result.result.image, symbol)
            changed = [(b, a) for b, a in zip(before, after) if a != b]
            self.assertTrue(changed, symbol)
            for b, a in changed:
                # сдвиг прижат к сетке 0.375° и равен заявленному
                self.assertAlmostEqual(a - b, round(shift / 0.375) * 0.375, places=6)
        # карты полной нагрузки не тронуты
        for symbol in ("ip_cam_sp_tco_2_in_fl__n", "ip_cam_sp_tco_2_ex_fl__n"):
            entry = self.policy.objects[symbol]
            region = range(entry.address, entry.address + entry.rows * entry.columns)
            for offset in region:
                self.assertEqual(self.image[offset], result.result.image[offset], symbol)

    def test_shift_above_limit_rejected(self) -> None:
        result = self.build([{"id": "v1", "kind": "vanos", "shift_in_deg": 7.0,
                              "evidence": GOOD_VANOS}])
        self.assertEqual("REJECTED", result.status)

    def test_missing_shift_rejected(self) -> None:
        result = self.build([{"id": "v1", "kind": "vanos", "evidence": GOOD_VANOS}])
        self.assertEqual("REJECTED", result.status)


class DisaOffTests(Base):
    """DISA off (шаг 2 задания): заслонка удерживается в одном положении.

    Функцию отключить нечем — карты задают положение; поэтому пакет удерживает
    выбранный клиентом код по всей рабочей зоне, а код ошибки привода гасится
    отдельным слоем диагностики.
    """

    SYMBOLS = ("id_vim_fl__n_vim", "id_vim_pl__n_vim__maf")

    def test_hold_position_is_written_to_both_tables(self) -> None:
        result = self.build([{"id": "d1", "kind": "disa_off", "hold": 1,
                              "acknowledge_mechanics": True}])
        self.assertEqual("BUILT", result.status, result.findings)
        package = next(p for p in result.packages if p.id == "disa_off")
        self.assertEqual("written", package.status)
        for symbol in self.SYMBOLS:
            before = self.cells(self.image, symbol)
            after = self.cells(result.result.image, symbol)
            self.assertTrue(any(a != b for b, a in zip(before, after)), symbol)
            self.assertEqual({0.0, 1.0}, set(before) | set(after), symbol)
            self.assertTrue(all(value == 1.0 for value in after), symbol)
        self.assertIn("DISA_OFF_NOTICE", self.codes(result))
        self.verify(result.result.image)

    def test_missing_hold_waits_for_the_customer_code(self) -> None:
        """Код положения — данные клиента, а не отказ заявки (шаг 2 задания)."""
        result = self.build([{"id": "d1", "kind": "disa_off",
                              "acknowledge_mechanics": True}])
        self.assertEqual("REQUIRES_DATA", result.status, result.findings)
        package = next(p for p in result.packages if p.id == "disa_off")
        self.assertGreaterEqual(len(package.needs), 3)
        self.assertIn("DISA_OFF_REQUIRES_DATA", self.codes(result))
        messages = " ".join(item["message"] for item in result.findings)
        self.assertIn("hold", messages)

    def test_hold_outside_stock_coding_is_rejected(self) -> None:
        result = self.build([{"id": "d1", "kind": "disa_off", "hold": 2,
                              "acknowledge_mechanics": True}])
        self.assertEqual("REJECTED", result.status)
        self.assertIn("REQUEST_INVALID", self.codes(result))

    def test_mechanics_must_be_acknowledged(self) -> None:
        result = self.build([{"id": "d1", "kind": "disa_off", "hold": 1}])
        self.assertEqual("REQUIRES_DATA", result.status, result.findings)
        self.assertIn("DISA_OFF_REQUIRES_DATA", self.codes(result))
        messages = " ".join(item["message"] for item in result.findings)
        self.assertIn("механики", messages)

    def test_protected_tables_untouched(self) -> None:
        result = self.build([{"id": "d1", "kind": "disa_off", "hold": 0,
                              "acknowledge_mechanics": True}])
        self.assertEqual("BUILT", result.status, result.findings)
        for symbol in ("c_n_hys_vim", "c_maf_hys_vim", "c_abc_inc_vim",
                       "ip_iga_ron_91_pl_ivvt__n__maf"):
            entry = self.policy.objects[symbol]
            stop = entry.address + entry.rows * entry.columns
            self.assertEqual(self.image[entry.address:stop],
                             result.result.image[entry.address:stop], symbol)


class DtcDriverTests(Base):
    """Слой диагностики привода (VIM/ISA) в общем пакете ``dtc``."""

    def test_vim_and_isa_counters_are_cleared(self) -> None:
        result = self.build(
            [{"id": "d1", "kind": "dtc", "groups": ["vim", "isa"],
              "acknowledge_mechanics": True}],
            acknowledge_legality=True,
        )
        self.assertEqual("BUILT", result.status, result.findings)
        for symbol in ("c_abc_inc_vim", "c_abc_inc_isa_1", "c_abc_inc_isa_2"):
            entry = self.policy.objects[symbol]
            before = stages._current(self.image, entry, 0)
            after = stages._current(result.result.image, entry, 0)
            self.assertGreater(before, 0, symbol)
            self.assertEqual(0.0, after, symbol)
        self.assertIn("DTC_DRIVER_WARNING", self.codes(result))
        self.verify(result.result.image)

    def test_without_legal_notice_is_rejected(self) -> None:
        result = self.build([{"id": "d1", "kind": "dtc", "groups": ["vim"],
                              "acknowledge_mechanics": True}],
                            acknowledge_legality=False)
        self.assertEqual("REJECTED", result.status)
        self.assertIn("ACKNOWLEDGE_LEGALITY_REQUIRED", self.codes(result))

    def test_without_mechanics_is_rejected(self) -> None:
        result = self.build([{"id": "d1", "kind": "dtc", "groups": ["isa"]}],
                            acknowledge_legality=True)
        self.assertEqual("REJECTED", result.status)
        self.assertIn("REQUEST_INVALID", self.codes(result))

    def test_catalyst_groups_still_require_no_catalyst(self) -> None:
        result = self.build([{"id": "d1", "kind": "dtc", "groups": ["cat"]}],
                            acknowledge_legality=True)
        self.assertEqual("REJECTED", result.status)
        self.assertIn("DTC_NO_CATALYST_REQUIRED", self.codes(result))

    def test_unknown_group_is_rejected(self) -> None:
        result = self.build([{"id": "d1", "kind": "dtc", "groups": ["egr"],
                              "acknowledge_mechanics": True}],
                            acknowledge_legality=True)
        self.assertEqual("REJECTED", result.status)
        self.assertIn("DTC_GROUP_UNKNOWN", self.codes(result))


class IsaOffWaitsForIdleLogTests(Base):
    """ISA off остаётся ``REQUIRES_DATA``: карты ХХ без лога не правим."""

    def test_isa_off_needs_idle_log_and_mechanics(self) -> None:
        result = self.build([{"id": "i1", "kind": "isa_off",
                              "acknowledge_mechanics": True}])
        self.assertEqual("REQUIRES_DATA", result.status)
        package = next(p for p in result.packages if p.id == "isa_off")
        self.assertEqual("requires_data", package.status)
        self.assertTrue(package.needs)
        text = " ".join(package.needs)
        self.assertIn("холостого", text)
        self.assertIn("dtc", text)


class ProtectionTests(Base):
    """Защищённая зона задания: пороги полной нагрузки и модель момента."""

    def test_protected_symbols_untouched(self) -> None:
        result = self.build([
            {"id": "i1", "kind": "ignition", "delta_deg": 1.5},
            {"id": "f1", "kind": "fuel", "tib_add_ms": 0.2},
            {"id": "v1", "kind": "vanos", "shift_in_deg": 2.0, "evidence": GOOD_VANOS},
        ])
        self.assertEqual("BUILT", result.status)
        for symbol in ("id_pvs_fl__n", "id_pvs_fl_ivvt__n", "ip_tqi_maf__n__maf",
                       "ip_tqi_pvs__n__pvs"):
            entry = self.policy.objects[symbol]
            region = range(entry.address,
                           entry.address + entry.rows * entry.columns
                           * (entry.element_size_bits // 8))
            for offset in region:
                self.assertEqual(self.image[offset], result.result.image[offset], symbol)
        # коэффициенты детонации в политику не входят вовсе (защита задания):
        # сверяем по адресам XDF, что байты целы
        from MS43.tools.xdf import load_definition
        definition = load_definition(
            Path(__file__).resolve().parents[1]
            / "definitions" / "Siemens_MS43_430069_Extended_Load_512K.xdf"
        )
        for symbol in ("ip_fac_knk_0__n__maf", "id_iga_dec_knk_1__n"):
            obj = definition.objects.get(symbol)
            if isinstance(obj, list):
                obj = obj[0]
            self.assertIsNotNone(obj, symbol)
            start = definition.base_offset + obj.address
            region = range(start, start + obj.byte_length)
            for offset in region:
                self.assertEqual(self.image[offset], result.result.image[offset], symbol)


class CrossVersionTests(Base):
    """Пакеты работают на всех покрытых версиях (политика выбирается по образу)."""

    def test_430037_stage1_with_new_packages(self) -> None:
        path = ARCHIVE / "E46_330i__b137954g_Ca430037_Stok.bin"
        image = path.read_bytes()
        self.assertEqual(0x80000, len(image))
        result = stages.build(
            "stage1",
            image,
            {
                "stage": "stage1",
                "customer": "тест версий",
                "intents": [
                    {"id": "i1", "kind": "ignition", "delta_deg": 1.5},
                    {"id": "f1", "kind": "fuel", "tib_add_ms": 0.1},
                ],
            },
        )
        self.assertEqual("BUILT", result.status)
        self.assertEqual("430037", result.cal_version)
        ids = {p.id: p.status for p in result.packages}
        self.assertEqual("written", ids["ignition"])
        self.assertEqual("written", ids["fuel"])


if __name__ == "__main__":
    unittest.main()


class VariantTests(Base):
    """Три варианта поставки (задание 3, веб-анализ §6)."""

    def test_unknown_variant_rejected(self) -> None:
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 1.0}],
                            variant="v9")
        self.assertEqual("REJECTED", result.status)
        self.assertIn("VARIANT_UNKNOWN", {f["code"] for f in result.findings})

    def test_v1_builds_small_ignition_and_tib(self) -> None:
        result = self.build(
            [
                {"id": "i1", "kind": "ignition", "delta_deg": 1.5},
                {"id": "f1", "kind": "fuel", "tib_add_ms": 0.1, "tib_add_percent": 4.0},
            ],
            variant="v1",
        )
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertEqual({"ignition", "fuel"}, {p.id for p in result.written_packages})

    def test_v1_rejects_big_ignition_and_enrichment(self) -> None:
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 2.5}],
                            variant="v1")
        self.assertEqual("REJECTED", result.status)
        result = self.build([{"id": "f1", "kind": "fuel", "ti_fl_enrich_lambda": 0.02}],
                            variant="v1")
        self.assertEqual("REJECTED", result.status)
        codes = {f["code"] for f in result.findings}
        self.assertIn("VARIANT_SCOPE_VIOLATION", codes)

    def test_v1_rejects_out_of_scope_intents(self) -> None:
        result = self.build([{"id": "v1", "kind": "vanos", "shift_in_deg": 2.0}],
                            variant="v1")
        self.assertEqual("REJECTED", result.status)
        codes = {f["code"] for f in result.findings}
        self.assertIn("VARIANT_SCOPE_VIOLATION", codes)

    def test_v2_allows_vanos_and_rpm_but_still_gates_logs(self) -> None:
        # ВАНос без лога — не отказ варианту: обороты пишутся, ВАНос ждёт
        # данных в наряде (файл собирается из готовых пакетов)
        result = self.build(
            [
                {"id": "v1", "kind": "vanos", "shift_in_deg": 2.0},
                {"id": "r1", "kind": "rpm_limit", "transmission": "manual",
                 "soft_rpm": 6688, "hard_rpm": 6720,
                 "acknowledge_risk": True, "acknowledge_oil_pump": True},
            ],
            variant="v2",
        )
        self.assertEqual("BUILT", result.status, result.findings)
        pending = {p.id for p in result.packages if p.status == "requires_data"}
        self.assertIn("vanos", pending)
        self.assertIn("limits", {p.id for p in result.written_packages})

    def test_v2_builds_full_set_with_logs(self) -> None:
        result = self.build(
            [
                {"id": "i1", "kind": "ignition", "delta_deg": 2.5, "evidence": GOOD_IGNITION},
                {"id": "f1", "kind": "fuel", "ti_fl_enrich_lambda": 0.03, "evidence": GOOD_FUEL},
                {"id": "v1", "kind": "vanos", "shift_in_deg": 2.0, "evidence": GOOD_VANOS},
            ],
            variant="v2",
        )
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertEqual({"ignition", "fuel", "vanos"},
                         {p.id for p in result.written_packages})

    def test_v3_requires_no_catalyst_and_warns(self) -> None:
        result = self.build(
            [{"id": "i1", "kind": "ignition", "delta_deg": 1.5}], variant="v3"
        )
        self.assertEqual("REJECTED", result.status)
        self.assertIn("NO_CATALYST_REQUIRED", {f["code"] for f in result.findings})
        result = self.build(
            [{"id": "i1", "kind": "ignition", "delta_deg": 1.5}],
            variant="v3", no_catalyst=True,
        )
        self.assertEqual("BUILT", result.status, result.findings)
        codes = {f["code"] for f in result.findings}
        self.assertIn("V3_LEGALITY_WARNING", codes)


class DtcLayerTests(Base):
    """Слой диагностики «без катализатора» (вариант 3)."""

    ALL_GROUPS = ["cat", "dmtl", "lambda", "sap"]

    def test_requires_no_catalyst(self) -> None:
        result = self.build([{"id": "d1", "kind": "dtc", "groups": self.ALL_GROUPS}])
        self.assertEqual("REJECTED", result.status)
        self.assertIn("DTC_NO_CATALYST_REQUIRED", {f["code"] for f in result.findings})

    def test_writes_zeros_and_keeps_front_sensor(self) -> None:
        result = self.build([{"id": "d1", "kind": "dtc", "groups": self.ALL_GROUPS}],
                            no_catalyst=True)
        self.assertEqual("BUILT", result.status, result.findings)
        after = result.result.image
        # подопытные счётчики стали нулями
        for symbol in ("c_abc_inc_conv_mon", "c_abc_inc_lsh_down",
                       "c_abc_inc_vls_down", "c_abc_inc_vls_down_t"):
            entry = self.policy.objects[symbol]
            self.assertEqual(0, stages._current(after, entry), symbol)
        # передний зонд и детонация не тронуты: этих символов нет в политике
        # вовсе (не в группах слоя), адреса берём из XDF
        from MS43.tools.xdf import load_definition
        definition = load_definition(
            Path(__file__).resolve().parents[1]
            / "definitions" / "Siemens_MS43_430069_Extended_Load_512K.xdf"
        )
        for symbol in ("c_abc_inc_lsh_up", "c_abc_inc_vls_up", "c_abc_inc_knk"):
            obj = definition.objects[symbol]
            obj = obj[0] if isinstance(obj, list) else obj
            start = definition.base_offset + obj.address
            self.assertEqual(self.image[start], after[start], symbol)
        codes = {f["code"] for f in result.findings}
        self.assertIn("DTC_LEGALITY_WARNING", codes)

    def test_unknown_group_rejected(self) -> None:
        result = self.build([{"id": "d1", "kind": "dtc", "groups": ["bogus"]}],
                            no_catalyst=True)
        self.assertEqual("REJECTED", result.status)
        self.assertIn("DTC_GROUP_UNKNOWN", {f["code"] for f in result.findings})

    def test_dtc_is_v3_scope_only(self) -> None:
        result = self.build([{"id": "d1", "kind": "dtc", "groups": ["cat"]}],
                            variant="v2", no_catalyst=True)
        self.assertEqual("REJECTED", result.status)
        self.assertIn("VARIANT_SCOPE_VIOLATION", {f["code"] for f in result.findings})
        result = self.build([{"id": "d1", "kind": "dtc", "groups": ["cat"]}],
                            variant="v3", no_catalyst=True)
        self.assertEqual("BUILT", result.status, result.findings)

    def test_dtc_on_other_version(self) -> None:
        from pathlib import Path as P
        path = P(__file__).resolve().parents[1] / "firmware_archive" / \
            "E46_330i__b137954g_Ca430037_Stok.bin"
        image = path.read_bytes()
        result = stages.build(
            "stage1", image,
            {"stage": "stage1", "customer": "тест", "no_catalyst": True,
             "acknowledge_legality": True,
             "intents": [{"id": "d1", "kind": "dtc", "groups": ["cat", "lambda"]}]},
        )
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertEqual("430037", result.cal_version)
        # по адресам политики 430037 счётчики обнулены
        from MS43.tools.policy import load_policy_for_version
        pol37 = load_policy_for_version("430037")
        for symbol in ("c_abc_inc_conv_mon", "c_abc_inc_lsh_down"):
            e = pol37.objects[symbol]
            self.assertEqual(0, stages._current(result.result.image, e), symbol)


class RealImageSweepTests(unittest.TestCase):
    """Независимая байтовая сверка по всем реальным образам (задание: проверка
    перед влитием). Для каждого стока собирается вариант 1 и проверяется: файл
    проходит пересчёт контрольных сумм, изменены только ячейки карт пакета."""

    def test_v1_over_every_430069_stock(self) -> None:
        firmware = Path(__file__).resolve().parents[1] / "firmware"
        images = sorted(firmware.glob("*.bin"))
        self.assertGreaterEqual(len(images), 18)
        request = {
            "stage": "stage1",
            "variant": "v1",
            "customer": "сверка",
            "intents": [
                {"id": "i1", "kind": "ignition", "delta_deg": 1.5},
                {"id": "f1", "kind": "fuel", "tib_add_ms": 0.1, "tib_add_percent": 4.0},
            ],
        }
        policy = load_policy()
        allowed = {
            "ip_iga_ron_98_pl_ivvt__n__maf",
            "ip_iga_optm_tco_2__n__maf",
            "ip_tib__n__maf",
        }
        import tempfile
        from MS43.tools import checksum as ck
        built = 0
        for path in images:
            image = path.read_bytes()
            result = stages.build("stage1", image, request, policy=policy)
            if not ck.verify_image(path).ok:
                # сток с испорченными суммами обязан быть отвергнут целиком
                self.assertEqual("REJECTED", result.status, path.name)
                self.assertIn("SOURCE_CRC_INVALID",
                              {f["code"] for f in result.findings}, path.name)
                continue
            built += 1
            self.assertEqual("BUILT", result.status, (path.name, result.findings))
            # изменены только карты пакета
            self.assertEqual(
                allowed, {item.symbol for item in result.plan.symbols}, path.name)
            # каждая ячейка внутри своего символа
            for item in result.plan.symbols:
                entry = policy.objects[item.symbol]
                end = entry.address + entry.rows * entry.columns * \
                    max(1, entry.element_size_bits // 8)
                for cell in item.cells:
                    self.assertLessEqual(entry.address, cell.address)
                    self.assertLess(cell.address, end, path.name)
            with tempfile.TemporaryDirectory() as directory:
                out = Path(directory) / "out.bin"
                out.write_bytes(result.result.image)
                report = ck.verify_image(out)
                self.assertTrue(report.ok, path.name)
        self.assertGreaterEqual(built, 17, "сверка должна собирать почти все стоки")

    def test_dtc_over_other_versions(self) -> None:
        from MS43.tools import checksum as ck
        import tempfile
        cases = {
            "430037": "E46_330i__b137954g_Ca430037_Stok.bin",
            "430055": "E46_330i__C155G50G_Ca430055_Stok.bin",
            "430056": "E46_330i__c256954g_Ca430056_Stok.bin",
            "430066": "E39_530__c6668528_Ca430066_Stok.bin",
        }
        archive = Path(__file__).resolve().parents[1] / "firmware_archive"
        for version, name in cases.items():
            path = archive / name
            image = path.read_bytes()
            self.assertEqual(0x80000, len(image), name)
            result = stages.build(
                "stage1", image,
                {"stage": "stage1", "customer": "сверка", "no_catalyst": True,
                 "acknowledge_legality": True,
                 "intents": [{"id": "d1", "kind": "dtc", "groups": ["cat", "lambda"]}]},
            )
            self.assertEqual("BUILT", result.status, (name, result.findings))
            self.assertEqual(version, result.cal_version, name)
            written = {item.symbol for item in result.plan.symbols}
            self.assertTrue(written, name)
            self.assertTrue(
                written <= {s for group in stages.DTC_GROUPS.values() for s in group},
                (name, written),
            )
            with tempfile.TemporaryDirectory() as directory:
                out = Path(directory) / "out.bin"
                out.write_bytes(result.result.image)
                self.assertTrue(ck.verify_image(out).ok, name)

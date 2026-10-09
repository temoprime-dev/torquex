"""Пакеты «зажигание / топливо / ВАНос» для MS45 (задание docs/TASK_PACKS_RU.md).

Ворота — те же, что у MS43 (единая спецификация ``docs/pack_log_requirements.json``):

* зажигание: добавка к карте ``ip_iga_bas_ron_98__n__maf``; без лога ≤ +1.5°,
  с логом детонации ≤ +3.0°; угол не убавляется; карта RON91 и защита по
  детонации не пишутся никогда; потолка ``ip_iga_optm_tco_2`` в семействе нет;
* топливо: обогащение ``ip_lamb_fl__n`` только с логом широкой лямбды и только
  там, где доказаны ворота диагностики богатой смеси (4570LO00S);
* ВАНос: только прогретые карты частичной нагрузки и только по логу фаз;
* семейства без доказанных адресов (4560BN00S, топливо на 4560BO00S) честно
  отвечают ``REQUIRES_DATA``.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MS45.tools import checksums as ck  # noqa: E402
from MS45.tools import policy as pl  # noqa: E402
from MS45.tools import xdf as xd  # noqa: E402
from MS45.tools import stages  # noqa: E402
from tools import pack_log_analyzer as analyzer  # noqa: E402
from tools import pack_log_fixtures as fx  # noqa: E402
from tools import pack_recommendation  # noqa: E402
from tools import variant1  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
MS45 = ROOT / "MS45"
BO00_CAL = MS45 / "firmware" / "MS45.0_456BO00S_E83" / "7561448_0044560BO00S0CO0R.bin"
LO00_CAL = MS45 / "firmware_spdaten" / "cal" / "MS45.1_0044570LO00S01J2R_P7561517_cal_116K.bin"
BN00_CAL = MS45 / "firmware_spdaten" / "cal" / "MS45.0_0044560BN00S07F0G_P7550088_cal_116K.bin"
SPEC = json.loads((ROOT / "docs" / "pack_log_requirements.json").read_text(encoding="utf-8"))
QUANTUM = 0.375

# Задание 1 ревью: доказательства создаёт только анализатор. Фикстуры —
# синтетические логи (реальные придут от клиентов); они пишутся во временный
# каталог один раз на сессию тестов и прогоняются через анализатор.
import tempfile  # noqa: E402

FIXTURE_DIR = Path(tempfile.mkdtemp(prefix="ms45_pack_log_"))
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
        cls.policy = pl.load_policy()

    def build(self, image: bytes, intents: list[dict], **extra) -> stages.StageResult:
        request = {"stage": "stage1", "customer": "тест", "intents": intents, **extra}
        # Правовая справка (задание docs/TASK_VARIANT1_RU.md §2) — предмет
        # отдельного теста; здесь подтверждение ставится, чтобы проверять сам
        # пакет, а не подтверждение.
        if any(item.get("kind") in variant1.LEGAL_GATE_KINDS for item in intents):
            request.setdefault("acknowledge_legality", True)
        return stages.build("stage1", image, request, policy=self.policy)

    def cells(self, image: bytes, family: str, symbol: str) -> list[float]:
        entry = self.policy.for_family(family).objects[symbol]
        cal, offset = ck.extract_cal(image)
        return [stages._current(image, entry, offset, i)
                for i in range(entry.rows * entry.columns)]


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
        # модульные алиасы генератора обязаны совпадать со спецификацией
        self.assertEqual(3.0, stages.IGNITION_DELTA_NO_LOG_DEG)
        self.assertEqual(5.0, stages.IGNITION_DELTA_MAX_DEG)
        self.assertEqual(0.375, stages.IGNITION_QUANTUM_DEG)
        self.assertEqual(98, SPEC["conditions"]["min_ron_for_ignition_evidence"])

    def test_missing_or_malformed_evidence(self) -> None:
        ok, problems = stages.validate_pack_evidence(None, "ignition")
        self.assertFalse(ok)
        self.assertTrue(problems)
        ok, _ = stages.validate_pack_evidence(dict(GOOD_IGNITION, kind="stub"), "ignition")
        self.assertFalse(ok)
        ok, problems = stages.validate_pack_evidence(dict(GOOD_IGNITION, package="fuel"), "ignition")
        self.assertFalse(ok)
        self.assertTrue(any("fuel" in item for item in problems))
        ok, problems = stages.validate_pack_evidence(dict(GOOD_IGNITION, source="  "), "ignition")
        self.assertFalse(ok)
        self.assertTrue(any("source" in item for item in problems))

    def test_each_threshold_is_checked(self) -> None:
        cases = [
            dict(GOOD_IGNITION, fuel_ron=92),
            dict(GOOD_IGNITION, results={"no_knock_fraction": 0.9,
                                         "max_sustained_retard_seconds": 0.1,
                                         "knock_spike_events": 0, "max_iat_c": 30.0}),
            dict(GOOD_IGNITION, results={"no_knock_fraction": 0.999,
                                         "max_sustained_retard_seconds": 0.9,
                                         "knock_spike_events": 0, "max_iat_c": 30.0}),
            dict(GOOD_FUEL, results={"lambda_stdev_wot": 0.2,
                                     "partial_load_trim_max_percent": 1.0,
                                     "lambda_floor": 0.86}),
            dict(GOOD_VANOS, results={"max_track_error_deg": 9.0, "min_hold_seconds": 5.0}),
        ]
        for bad in cases:
            ok, problems = stages.validate_pack_evidence(bad, bad["package"])
            self.assertFalse(ok, bad)
            self.assertTrue(problems)


class AnalyzerGateTests(Base):
    """Задание 1 ревью: ворота принимают только доказательство анализатора."""

    def setUp(self) -> None:
        self.image = BO00_CAL.read_bytes()

    def test_manual_json_is_rejected_with_own_code(self) -> None:
        manual = evidence(
            "ignition", no_knock_fraction=0.999,
            max_sustained_retard_seconds=0.1,
            knock_spike_events=0, max_iat_c=30.0,
        )
        result = self.build(self.image, [{"id": "i1", "kind": "ignition",
                                          "delta_deg": 2.5, "evidence": manual}])
        self.assertEqual("REQUIRES_DATA", result.status)
        package = next(p for p in result.packages if p.id == "ignition")
        self.assertIn(analyzer.EVIDENCE_MANUAL_JSON, package.detail)

    def test_tampered_results_are_rejected(self) -> None:
        tampered = dict(GOOD_IGNITION)
        tampered["results"] = dict(GOOD_IGNITION["results"],
                                   knock_spike_events=3)
        result = self.build(self.image, [{"id": "i1", "kind": "ignition",
                                          "delta_deg": 2.5, "evidence": tampered}])
        self.assertEqual("REQUIRES_DATA", result.status)
        package = next(p for p in result.packages if p.id == "ignition")
        self.assertIn(analyzer.EVIDENCE_TAMPERED, package.detail)

    def test_missing_source_log_is_rejected(self) -> None:
        lost = dict(GOOD_IGNITION, source="нет/такого/файла.csv")
        result = self.build(self.image, [{"id": "i1", "kind": "ignition",
                                          "delta_deg": 2.5, "evidence": lost}])
        self.assertEqual("REQUIRES_DATA", result.status)
        package = next(p for p in result.packages if p.id == "ignition")
        self.assertIn(analyzer.EVIDENCE_SOURCE_NOT_FOUND, package.detail)

    def test_analyzer_evidence_passes_the_gate(self) -> None:
        ok, problems = analyzer.verify_evidence(GOOD_IGNITION, "ignition")
        self.assertTrue(ok, problems)


class AnalyzerBoundaryTests(unittest.TestCase):
    """Синтетические фикстуры на граничные случаи (задание 1 ревью).

    Пороговые проверки анализатора общие для семейств; здесь — те же
    фикстуры, что в MS43, плюс сценарий «плохой лог не пускает добавку»
    на образе MS45.
    """

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

    def test_hot_intake_invalidates_the_log(self) -> None:
        csv_path = fx.write_ignition_csv(self.dir / "hot.csv", hot_iat=True)
        outcome = analyzer.analyze_pack_log(csv_path, "ignition", 98)
        self.assertFalse(outcome["valid"])
        self.assertGreater(outcome["results"]["max_iat_c"],
                           SPEC["conditions"]["intake_temp_max_c"])

    def test_lean_wot_lambda_invalidates_fuel(self) -> None:
        csv_path = fx.write_fuel_csv(self.dir / "lean.csv", lean=True)
        outcome = analyzer.analyze_pack_log(csv_path, "fuel")
        self.assertFalse(outcome["valid"])
        self.assertLess(outcome["results"]["lambda_floor"],
                        SPEC["packages"]["fuel"]["criteria"]["lambda_target_min"])

    def test_vanos_track_error_invalidates_the_grid(self) -> None:
        csv_path = fx.write_vanos_csv(self.dir / "track.csv", track_error_deg=5.0)
        outcome = analyzer.analyze_pack_log(csv_path, "vanos")
        self.assertFalse(outcome["valid"])
        self.assertGreater(outcome["results"]["max_track_error_deg"],
                           SPEC["packages"]["vanos"]["criteria"]["max_track_error_deg"])

    def test_invalid_ignition_log_keeps_add_at_variant_one(self) -> None:
        image = BO00_CAL.read_bytes()
        csv_path = fx.write_ignition_csv(self.dir / "knock.csv", knock=True)
        proof = fx.evidence_from_csv(csv_path, "ignition")
        request = {"stage": "stage1", "customer": "тест",
                   "intents": [{"id": "i1", "kind": "ignition", "delta_deg": 2.5,
                                "evidence": proof}]}
        result = stages.build("stage1", image, request, policy=pl.load_policy())
        self.assertEqual("REQUIRES_DATA", result.status)


class IgnitionTests(Base):
    def test_small_add_without_log_is_written(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "i1", "kind": "ignition", "delta_deg": 1.5}])
        self.assertEqual("BUILT", result.status)
        before = self.cells(image, "4560BO00S", "ip_iga_bas_ron_98__n__maf")
        after = self.cells(result.result.image, "4560BO00S", "ip_iga_bas_ron_98__n__maf")
        changed = [(b, a) for b, a in zip(before, after) if a != b]
        self.assertTrue(changed)
        for b, a in changed:
            self.assertGreater(a, b)  # угол не убавляется
            self.assertLessEqual(a - b, 1.5 + QUANTUM / 2 + 1e-9)
            self.assertAlmostEqual((a - b) % QUANTUM, 0.0, places=6)  # квант сетки

    def test_ron91_and_knock_protection_untouched(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "i1", "kind": "ignition", "delta_deg": 1.5}])
        self.assertEqual("BUILT", result.status)
        for symbol in ("ip_iga_bas_ron_91__n__maf", "ip_iga_bas_max_knk__n__maf",
                       "ip_iga_bas__n__maf"):
            entry = self.policy.for_family("4560BO00S").objects[symbol]
            cal, offset = ck.extract_cal(image)
            start = offset + entry.address
            stop = start + entry.rows * entry.columns * entry.element_size_bytes
            self.assertEqual(image[start:stop], result.result.image[start:stop], symbol)

    def test_big_add_requires_log(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "i1", "kind": "ignition", "delta_deg": 2.5}])
        self.assertEqual("REQUIRES_DATA", result.status)
        package = next(p for p in result.packages if p.id == "ignition")
        self.assertEqual("requires_data", package.status)
        self.assertTrue(package.needs)

    def test_big_add_with_log_is_written(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "i1", "kind": "ignition", "delta_deg": 2.5,
                                     "evidence": GOOD_IGNITION}])
        self.assertEqual("BUILT", result.status)
        before = self.cells(image, "4560BO00S", "ip_iga_bas_ron_98__n__maf")
        after = self.cells(result.result.image, "4560BO00S", "ip_iga_bas_ron_98__n__maf")
        changed = [(b, a) for b, a in zip(before, after) if a != b]
        self.assertTrue(changed)
        for b, a in changed:
            self.assertLessEqual(a - b, 2.5 + QUANTUM / 2 + 1e-9)

    def test_low_ron_evidence_is_rejected(self) -> None:
        image = BO00_CAL.read_bytes()
        bad = dict(GOOD_IGNITION, fuel_ron=92)
        result = self.build(image, [{"id": "i1", "kind": "ignition", "delta_deg": 2.5,
                                     "evidence": bad}])
        self.assertEqual("REQUIRES_DATA", result.status)


class Stage1CeilingTests(Base):
    """Границы Stage 1/Stage 2 из единой спецификации (задание §2.1).

    Те же границы, что у MS43: без логов +3,0° (RON98) / +1,5° (RON95), с
    логом детонации +5,0°. Раскладка по запасу до MBT в MS45 — REQUIRES_DATA
    (аналог карты не доказан), поэтому проверяем только потолок и форму.
    """

    def deltas(self, image: bytes, result: stages.StageResult) -> list[float]:
        before = self.cells(image, "4560BO00S", "ip_iga_bas_ron_98__n__maf")
        after = self.cells(result.result.image, "4560BO00S",
                           "ip_iga_bas_ron_98__n__maf")
        return [a - b for b, a in zip(before, after)]

    def test_ron98_three_degrees_without_log_is_written(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "i1", "kind": "ignition",
                                     "delta_deg": 3.0}], octane="ron98")
        self.assertEqual("BUILT", result.status, result.findings)
        deltas = self.deltas(image, result)
        self.assertTrue(any(d > 0 for d in deltas))
        self.assertLessEqual(max(deltas), 3.0 + 1e-9)

    def test_ron98_above_three_needs_the_log(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "i1", "kind": "ignition",
                                     "delta_deg": 3.01}], octane="ron98")
        self.assertEqual("REQUIRES_DATA", result.status, result.findings)
        package = next(p for p in result.packages if p.id == "ignition")
        self.assertIn("требует лога детонации", package.detail)

    def test_five_degrees_with_log_is_written(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "i1", "kind": "ignition",
                                     "delta_deg": 5.0, "evidence": GOOD_IGNITION}],
                            octane="ron98")
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertLessEqual(max(self.deltas(image, result)), 5.0 + 1e-9)

    def test_above_five_is_refused_even_with_log(self) -> None:
        image = BO00_CAL.read_bytes()
        for delta in (5.01, 6.0):
            result = self.build(image, [{"id": "i1", "kind": "ignition",
                                         "delta_deg": delta,
                                         "evidence": GOOD_IGNITION}], octane="ron98")
            self.assertEqual("REJECTED", result.status, delta)
            # право профиля отсекает раньше пакета — код находки либо от
            # профиля (OCTANE_PROFILE_LIMIT), либо от пакета (REQUEST_INVALID)
            codes = {f["code"] for f in result.findings}
            self.assertTrue({"OCTANE_PROFILE_LIMIT", "REQUEST_INVALID"} & codes, codes)

    def test_ron95_boundary_is_one_and_a_half(self) -> None:
        image = BO00_CAL.read_bytes()
        ok = self.build(image, [{"id": "i1", "kind": "ignition", "delta_deg": 1.5}],
                        octane="ron95")
        self.assertEqual("BUILT", ok.status, ok.findings)
        above = self.build(image, [{"id": "i1", "kind": "ignition",
                                    "delta_deg": 1.51}], octane="ron95")
        self.assertEqual("REQUIRES_DATA", above.status, above.findings)

    def test_mbt_layout_reports_requires_data(self) -> None:
        """Раскладка по MBT в MS45 не применяется, но честно названа в находках."""
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "i1", "kind": "ignition",
                                     "delta_deg": 1.5}], octane="ron98")
        self.assertEqual("BUILT", result.status, result.findings)
        mbt = [f for f in result.findings
               if f["code"] == "MBT_LAYOUT_REQUIRES_DATA"]
        self.assertEqual(1, len(mbt))
        self.assertIn("ip_iga_optm_tco_2", mbt[0]["message"])
        self.assertIn("ip_iga_ref__n__maf", mbt[0]["message"])
        detail = next(p for p in result.packages if p.id == "ignition").detail
        self.assertIn("REQUIRES_DATA", detail)

    def test_neighbour_column_steps_are_bounded(self) -> None:
        from tools import ignition_caps, ignition_layout

        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "i1", "kind": "ignition",
                                     "delta_deg": 3.0}], octane="ron98")
        self.assertEqual("BUILT", result.status, result.findings)
        axis = stages._policy_axis(image, self.policy.for_family("4560BO00S"), 0, stages.IGN_LOAD_AXIS)
        entry = self.policy.for_family("4560BO00S").objects["ip_iga_bas_ron_98__n__maf"]
        column_adds = ignition_layout.smooth_1d([0.0] * 16)  # тип-страховка, не нужен
        maxima: list[float] = []
        deltas = self.deltas(image, result)
        for col in range(entry.columns):
            maxima.append(max(deltas[col::entry.columns]))
        step = ignition_caps.layout()["neighbour_max_step_deg"]
        for left, right in zip(maxima, maxima[1:]):
            self.assertLessEqual(abs(right - left), step + 1e-9)
        # форма: ниже 200 мг/такт добавок нет
        for col, load in enumerate(axis):
            if load <= stages.IGNITION_SHAPE_LOAD_LOW_MG:
                self.assertEqual(0.0, maxima[col], f"колонка {col}, {load} мг/такт")
        del column_adds


class IgnitionShapeTests(Base):
    """Задание 3 ревью: добавка зажигания не плоская, а по форме нагрузки.

    Ниже 200 мг/такт (ХХ/ПХХ/лёгкий круиз) добавка нулевая, далее линейный
    рост, с 600 мг/такт (полная нагрузка) — полная добавка. Обоснование и
    источники — ``docs/PACKS_WEB_ANALYSIS_RU.md`` §7.
    """

    def _axis(self, image: bytes, family: str = "4560BO00S") -> list[float]:
        axis = stages._policy_axis(image, self.policy.for_family(family), 0, stages.IGN_LOAD_AXIS)
        self.assertIsNotNone(axis)
        return axis

    def test_shape_factor_is_zero_low_full_high_linear_mid(self) -> None:
        self.assertEqual(0.0, stages.ignition_shape_factor(50.0))
        self.assertEqual(0.0, stages.ignition_shape_factor(200.0))
        self.assertAlmostEqual(0.5, stages.ignition_shape_factor(400.0), places=9)
        self.assertEqual(1.0, stages.ignition_shape_factor(600.0))
        self.assertEqual(1.0, stages.ignition_shape_factor(1300.0))

    def test_axis_is_found_on_every_proven_family(self) -> None:
        # ось берётся из политики семейства (адрес XDF), а не поиском по образу:
        # прежний поиск находил посторонний ряд 32,6…1351 мг/такт
        for path, family in ((BO00_CAL, "4560BO00S"), (LO00_CAL, "4570LO00S")):
            axis = self._axis(path.read_bytes(), family)
            self.assertEqual(16, len(axis), path.name)
            self.assertTrue(all(a < b for a, b in zip(axis, axis[1:])), path.name)
            self.assertLess(axis[0], 150.0, path.name)
            self.assertTrue(500.0 <= axis[-1] <= 750.0, f"{path.name}: {axis[-1]}")

    def test_add_is_shaped_by_load(self) -> None:
        image = BO00_CAL.read_bytes()
        axis = self._axis(image)
        result = self.build(image, [{"id": "i1", "kind": "ignition", "delta_deg": 2.5,
                                     "evidence": GOOD_IGNITION}])
        self.assertEqual("BUILT", result.status)
        before = self.cells(image, "4560BO00S", "ip_iga_bas_ron_98__n__maf")
        after = self.cells(result.result.image, "4560BO00S",
                           "ip_iga_bas_ron_98__n__maf")
        entry = self.policy.for_family("4560BO00S").objects[
            "ip_iga_bas_ron_98__n__maf"]
        full_add = int(2.5 / QUANTUM + 1e-9) * QUANTUM  # прижатие ВНИЗ к сетке
        column_adds: dict[int, set] = {col: set() for col in range(entry.columns)}
        for index, (b, a) in enumerate(zip(before, after)):
            column = index % entry.columns
            self.assertGreaterEqual(a, b)  # угол не убавляется
            column_adds[column].add(round(a - b, 6))
        # за сеткой зон лога (нагрузка ≥ 700 мг/такт) генератор держит
        # консервативный потолок без лога — раскладка §7.3
        band = pack_recommendation.deltas_for_load_axis(
            GOOD_IGNITION["pack_recommendation"], axis, fallback_deg=1.5)
        for column, load in enumerate(axis):
            adds = column_adds[column]
            ceiling = int(min(2.5, band[column]) / QUANTUM + 1e-9) * QUANTUM
            if load <= stages.IGNITION_SHAPE_LOAD_LOW_MG:
                # малая нагрузка: ни одна ячейка не тронута
                self.assertEqual({0.0}, adds, f"колонка {column}, {load} мг/такт")
            elif load >= stages.IGNITION_SHAPE_LOAD_HIGH_MG:
                # полная нагрузка: добавка по потолку колонки (прижатая к сетке)
                self.assertEqual({ceiling}, adds, f"колонка {column}, {load} мг/такт")
            else:
                # середина: добавка не больше полной и не отрицательна
                self.assertTrue(all(0.0 <= add <= full_add + QUANTUM / 2 + 1e-9
                                    for add in adds), (column, adds))
        # внутри сетки зон добавка не убывает с ростом нагрузки; за сеткой
        # (нагрузка ≥ 700 мг/такт) держится консервативный потолок без лога
        maxima = [max(column_adds[col]) for col in range(entry.columns)]
        inside = [value for col, value in enumerate(maxima)
                  if pack_recommendation.load_band_index(axis[col]) is not None]
        self.assertEqual(inside, sorted(inside), (axis, maxima))
        beyond = [col for col, load in enumerate(axis) if load >= 700.0]
        self.assertTrue(beyond, "сетка зон покрыла всю ось — тест не проверяет пол")
        self.assertEqual({round(1.5 / QUANTUM) * QUANTUM},
                         {maxima[col] for col in beyond}, maxima)
        # затронуты не все ячейки: малые нагрузки пропущены
        changed = sum(1 for b, a in zip(before, after) if a != b)
        self.assertLess(changed, entry.rows * entry.columns)
        self.assertGreater(changed, 0)

    def test_small_add_is_shaped_too(self) -> None:
        image = BO00_CAL.read_bytes()
        axis = self._axis(image)
        result = self.build(image, [{"id": "i1", "kind": "ignition", "delta_deg": 1.5}])
        self.assertEqual("BUILT", result.status)
        before = self.cells(image, "4560BO00S", "ip_iga_bas_ron_98__n__maf")
        after = self.cells(result.result.image, "4560BO00S",
                           "ip_iga_bas_ron_98__n__maf")
        low_columns = [c for c, load in enumerate(axis)
                       if load <= stages.IGNITION_SHAPE_LOAD_LOW_MG]
        self.assertTrue(low_columns)
        for index, (b, a) in enumerate(zip(before, after)):
            if index % 16 in low_columns:
                self.assertEqual(b, a, f"ячейка {index} в зоне малой нагрузки")

    def test_delta_bounds(self) -> None:
        image = BO00_CAL.read_bytes()
        # 3.5 без логов — не отказ, а «принеси лог детонации» (Stage 2);
        # за пределом права стадии (5.0) — отказ даже с логом
        for bad_delta in (0, -1, 5.01):
            result = self.build(image, [{"id": "i1", "kind": "ignition", "delta_deg": bad_delta}])
            self.assertEqual("REJECTED", result.status, bad_delta)

    def test_bn00s_has_no_proven_addresses(self) -> None:
        image = BN00_CAL.read_bytes()
        result = self.build(image, [{"id": "i1", "kind": "ignition", "delta_deg": 1.5}])
        package = next(p for p in result.packages if p.id == "ignition")
        self.assertEqual("requires_data", package.status)
        self.assertIn("не доказаны", package.detail)


class FuelTests(Base):
    def test_bo00s_fuel_is_requires_data(self) -> None:
        # у семейства нет доказанных ворот диагностики богатой смеси
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "f1", "kind": "fuel",
                                     "ti_fl_enrich_lambda": 0.03, "evidence": GOOD_FUEL}])
        package = next(p for p in result.packages if p.id == "fuel")
        self.assertEqual("requires_data", package.status)
        self.assertIn("ворот", package.detail)

    def test_enrichment_without_log_waits(self) -> None:
        image = LO00_CAL.read_bytes()
        result = self.build(image, [{"id": "f1", "kind": "fuel", "ti_fl_enrich_lambda": 0.03}])
        self.assertEqual("REQUIRES_DATA", result.status)

    def test_enrichment_with_log_respects_floor_and_guard(self) -> None:
        image = LO00_CAL.read_bytes()
        result = self.build(image, [{"id": "f1", "kind": "fuel", "ti_fl_enrich_lambda": 0.03,
                                     "evidence": GOOD_FUEL}])
        self.assertEqual("BUILT", result.status)
        family = "4570LO00S"
        before = self.cells(image, family, "ip_lamb_fl__n")
        after = self.cells(result.result.image, family, "ip_lamb_fl__n")
        guard = self.cells(image, family, "c_lamb_min_ls_act_test_afr")[0]
        self.assertTrue(any(a != b for b, a in zip(before, after)))
        for b, a in zip(before, after):
            self.assertLessEqual(a, b + 1e-9)          # только обогащение
            self.assertGreaterEqual(a, guard - 1e-6)   # не ниже ворот диагностики
            self.assertGreaterEqual(a, 0.82 - 1e-6)    # пол 0.82
            if a != b:
                self.assertLessEqual(b - a, 0.03 + 0.001)

    def test_empty_request_waits_for_data(self) -> None:
        image = LO00_CAL.read_bytes()
        result = self.build(image, [{"id": "f1", "kind": "fuel"}])
        self.assertEqual("REQUIRES_DATA", result.status)

    def test_enrichment_above_limit_rejected(self) -> None:
        image = LO00_CAL.read_bytes()
        result = self.build(image, [{"id": "f1", "kind": "fuel",
                                     "ti_fl_enrich_lambda": 0.2, "evidence": GOOD_FUEL}])
        self.assertEqual("REJECTED", result.status)


class VanosTests(Base):
    def test_shift_requires_phase_log(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "v1", "kind": "vanos", "shift_in_deg": 2.0}])
        self.assertEqual("REQUIRES_DATA", result.status)
        package = next(p for p in result.packages if p.id == "vanos")
        self.assertEqual("requires_data", package.status)
        self.assertTrue(package.needs)

    def test_shift_with_log_is_snapped_to_grid(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "v1", "kind": "vanos", "shift_in_deg": 2.0,
                                     "shift_ex_deg": -2.0, "evidence": GOOD_VANOS}])
        self.assertEqual("BUILT", result.status)
        self.assertTrue(ck.verify_bytes(result.result.image).ok)
        for symbol, shift in (
            ("ip_cam_sp_2_in_pl__n__maf_ivvt", 2.0),
            ("ip_cam_sp_2_ex_pl__n__maf_ivvt", -2.0),
        ):
            before = self.cells(image, "4560BO00S", symbol)
            after = self.cells(result.result.image, "4560BO00S", symbol)
            expected = round(shift / QUANTUM) * QUANTUM  # 2° -> 1.875° по сетке
            changed = [(b, a) for b, a in zip(before, after) if a != b]
            self.assertTrue(changed, symbol)
            for b, a in changed:
                self.assertAlmostEqual(a - b, expected, places=6)
        # холодные и полнонагрузочные карты фаз не тронуты
        for symbol in ("ip_cam_sp_1_in_pl__n__maf_ivvt", "ip_cam_sp_2_in_fl__n",
                       "ip_cam_sp_2_ex_fl__n"):
            entry = self.policy.for_family("4560BO00S").objects[symbol]
            cal, offset = ck.extract_cal(image)
            start = offset + entry.address
            stop = start + entry.rows * entry.columns * entry.element_size_bytes
            self.assertEqual(image[start:stop], result.result.image[start:stop], symbol)

    def test_shift_above_limit_rejected(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "v1", "kind": "vanos", "shift_in_deg": 7.0,
                                     "evidence": GOOD_VANOS}])
        self.assertEqual("REJECTED", result.status)

    def test_missing_shift_rejected(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "v1", "kind": "vanos", "evidence": GOOD_VANOS}])
        self.assertEqual("REJECTED", result.status)

    def test_bn00s_has_no_proven_addresses(self) -> None:
        image = BN00_CAL.read_bytes()
        result = self.build(image, [{"id": "v1", "kind": "vanos", "shift_in_deg": 2.0,
                                     "evidence": GOOD_VANOS}])
        package = next(p for p in result.packages if p.id == "vanos")
        self.assertEqual("requires_data", package.status)


class CombinedTests(Base):
    """Вариант сборки «всё сразу с логами»: файл валиден, защита не тронута."""

    def test_full_request_builds_and_keeps_protections(self) -> None:
        image = LO00_CAL.read_bytes()
        result = self.build(image, [
            {"id": "i1", "kind": "ignition", "delta_deg": 1.5},
            {"id": "f1", "kind": "fuel", "ti_fl_enrich_lambda": 0.02, "evidence": GOOD_FUEL},
            {"id": "v1", "kind": "vanos", "shift_in_deg": 1.5, "shift_ex_deg": -1.5,
             "evidence": GOOD_VANOS},
        ])
        self.assertEqual("BUILT", result.status, [f["code"] for f in result.findings])
        self.assertEqual({"ignition", "fuel", "vanos"},
                         {p.id for p in result.written_packages})
        self.assertTrue(ck.verify_bytes(result.result.image).ok)
        # защищённые символы семейства не изменены
        for symbol in ("ip_iga_bas_ron_91__n__maf", "ip_iga_bas_max_knk__n__maf",
                       "c_lamb_min_ls_act_test_afr", "ip_cam_sp_2_in_fl__n"):
            entry = self.policy.for_family("4570LO00S").objects[symbol]
            cal, offset = ck.extract_cal(image)
            start = offset + entry.address
            stop = start + entry.rows * entry.columns * entry.element_size_bytes
            self.assertEqual(image[start:stop], result.result.image[start:stop], symbol)


class DtcDriverTests(Base):
    """Слой диагностики привода DISA (VIM) и клапана ХХ (ISA) — шаг 2 задания.

    Гасится только приращение счётчика дребезга; максимум счётчика и карты
    положения/ХХ не трогаются. Ворота: правовая справка и подтверждение
    механики — код при живом приводе не скрываем.
    """

    def test_counters_are_cleared_for_both_families(self) -> None:
        for path, family in ((BO00_CAL, "4560BO00S"), (LO00_CAL, "4570LO00S")):
            image = path.read_bytes()
            result = self.build(
                image,
                [{"id": "d1", "kind": "dtc_driver", "groups": ["vim", "isa"],
                  "acknowledge_mechanics": True}],
                acknowledge_legality=True, variant="v1",
            )
            self.assertEqual("BUILT", result.status, (family, result.findings))
            for symbol in ("c_det_inc_vim", "c_det_inc_isa_1", "c_det_inc_isa_2"):
                entry = self.policy.for_family(family).objects[symbol]
                before = stages._current(image, entry, 0)
                after = stages._current(result.result.image, entry, 0)
                self.assertEqual(1.0, before, (family, symbol))
                self.assertEqual(0.0, after, (family, symbol))
            # максимумы счётчиков (в политике их нет — только чтение по XDF)
            # и защищённые карты не тронуты
            definition = xd.load_definition(family=family, variant="116K")
            for symbol in ("c_det_max_vim", "c_det_max_isa_1", "c_det_max_isa_2"):
                obj = definition.find(symbol)
                stop = obj.address + obj.byte_length
                self.assertEqual(image[obj.address:stop],
                                 result.result.image[obj.address:stop],
                                 (family, symbol))
            for symbol in ("ip_iga_bas_ron_91__n__maf", "ip_iga_bas_max_knk__n__maf"):
                entry = self.policy.for_family(family).objects[symbol]
                stop = entry.address + entry.rows * entry.columns * entry.element_size_bytes
                self.assertEqual(image[entry.address:stop],
                                 result.result.image[entry.address:stop],
                                 (family, symbol))
            self.assertTrue(ck.verify_bytes(result.result.image).ok, family)

    def test_legal_notice_is_required(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "d1", "kind": "dtc_driver", "groups": ["vim"],
                                     "acknowledge_mechanics": True}],
                            acknowledge_legality=False, variant="v1")
        self.assertEqual("REJECTED", result.status)
        self.assertIn("ACKNOWLEDGE_LEGALITY_REQUIRED",
                      {f["code"] for f in result.findings})

    def test_mechanics_acknowledgement_is_required(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "d1", "kind": "dtc_driver", "groups": ["isa"]}],
                            acknowledge_legality=True, variant="v1")
        self.assertEqual("REJECTED", result.status)
        self.assertIn("REQUEST_INVALID", {f["code"] for f in result.findings})

    def test_unknown_group_is_rejected(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "d1", "kind": "dtc_driver", "groups": ["egr"],
                                     "acknowledge_mechanics": True}],
                            acknowledge_legality=True, variant="v1")
        self.assertEqual("REJECTED", result.status)
        self.assertIn("DTC_GROUP_UNKNOWN", {f["code"] for f in result.findings})


class DisaOffWaitsForDataTests(Base):
    """MS45: «DISA off» положением не выпускается — honest REQUIRES_DATA."""

    def test_disa_off_lists_what_is_needed(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "d1", "kind": "disa_off"}],
                            acknowledge_legality=True, variant="v1")
        package = next(p for p in result.packages if p.id == "disa_off")
        self.assertEqual("requires_data", package.status)
        self.assertTrue(package.needs)
        text = " ".join(package.needs) + " " + package.detail
        self.assertIn("vim", text)
        self.assertIn("dtc_driver", text)
        # подсказка клиенту: подавление счётчика доступно отдельным слоем
        self.assertIn("dtc_driver", text)


if __name__ == "__main__":
    unittest.main()


class VariantTests(Base):
    """Три варианта поставки (задание 3, веб-анализ §6)."""

    def test_unknown_variant_rejected(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "i1", "kind": "ignition", "delta_deg": 1.0}],
                            variant="v9")
        self.assertEqual("REJECTED", result.status)
        self.assertIn("VARIANT_UNKNOWN", {f["code"] for f in result.findings})

    def test_v1_ignition_only(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "i1", "kind": "ignition", "delta_deg": 1.5}],
                            variant="v1")
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertEqual({"ignition"}, {p.id for p in result.written_packages})

    def test_v1_rejects_fuel_and_big_ignition(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "i1", "kind": "ignition", "delta_deg": 2.5}],
                            variant="v1")
        self.assertEqual("REJECTED", result.status)
        image = LO00_CAL.read_bytes()
        result = self.build(image, [{"id": "f1", "kind": "fuel",
                                     "ti_fl_enrich_lambda": 0.02, "evidence": GOOD_FUEL}],
                            variant="v1")
        self.assertEqual("REJECTED", result.status)
        self.assertIn("VARIANT_SCOPE_VIOLATION", {f["code"] for f in result.findings})

    def test_v2_rev_limit_needs_oil_pump_ack(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(
            image,
            [{"id": "r1", "kind": "rev_limit", "value": 6800, "transmission": "mt",
              "acknowledge_risk": True}],
            variant="v2",
        )
        self.assertEqual("REJECTED", result.status)
        self.assertIn("ACKNOWLEDGE_REQUIRED", {f["code"] for f in result.findings})
        result = self.build(
            image,
            [{"id": "r1", "kind": "rev_limit", "value": 6800, "transmission": "mt",
              "acknowledge_risk": True, "acknowledge_oil_pump": True}],
            variant="v2",
        )
        self.assertEqual("BUILT", result.status, result.findings)

    def test_v3_requires_no_catalyst_and_warns(self) -> None:
        image = LO00_CAL.read_bytes()
        intents = [{"id": "i1", "kind": "ignition", "delta_deg": 1.5}]
        result = self.build(image, intents, variant="v3")
        self.assertEqual("REJECTED", result.status)
        self.assertIn("NO_CATALYST_REQUIRED", {f["code"] for f in result.findings})
        result = self.build(image, intents, variant="v3", no_catalyst=True)
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertIn("V3_LEGALITY_WARNING", {f["code"] for f in result.findings})


class RealImageSweepTests(unittest.TestCase):
    """Независимая байтовая сверка по всем реальным калибровкам семейства
    (задание: проверка перед влитием): вариант 1 собирается на каждом стоке,
    суммы пересчитаны, правка не выходит за карты пакета."""

    def test_v1_over_every_stock(self) -> None:
        policy = pl.load_policy()
        total = built = 0
        for family in ("4560BO00S", "4570LO00S"):
            for name, image in pl.stock_images(family):
                total += 1
                result = stages.build(
                    "stage1", image,
                    {"stage": "stage1", "variant": "v1", "customer": "сверка",
                     "intents": [{"id": "i1", "kind": "ignition", "delta_deg": 1.5}]},
                    policy=policy,
                )
                self.assertEqual("BUILT", result.status, (family, name, result.findings))
                written = {item.symbol for item in result.plan.symbols}
                self.assertEqual({"ip_iga_bas_ron_98__n__maf"}, written, name)
                entry = policy.for_family(family).objects["ip_iga_bas_ron_98__n__maf"]
                end = entry.address + entry.rows * entry.columns
                for item in result.plan.symbols:
                    for cell in item.cells:
                        self.assertLessEqual(entry.address, cell.cal_offset)
                        self.assertLess(cell.cal_offset, end, name)
                self.assertTrue(ck.verify_bytes(result.result.image).ok, name)
                built += 1
        self.assertGreaterEqual(built, 100)
        self.assertEqual(total, built)


class CatlessLayerTests(Base):
    """Слой «без катализатора» (вариант 3): удаление диагностики катализатора
    и продувки только по подтверждению и только по доказанным флагам."""

    def test_requires_no_catalyst(self) -> None:
        image = BO00_CAL.read_bytes()
        for kind, code in (("cat_delete", "CAT_DELETE_NO_CATALYST_REQUIRED"),
                           ("sap_delete", "SAP_DELETE_NO_CATALYST_REQUIRED")):
            result = self.build(image, [{"id": "x", "kind": kind}])
            self.assertEqual("REJECTED", result.status, kind)
            self.assertIn(code, {f["code"] for f in result.findings}, kind)

    def test_cat_delete_writes_flags(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(image, [{"id": "c", "kind": "cat_delete"}], no_catalyst=True)
        self.assertEqual("BUILT", result.status, result.findings)
        family = "4560BO00S"
        for symbol, expected in stages.CAT_DELETE_TARGETS.items():
            got = self.cells(result.result.image, family, symbol)
            self.assertEqual(list(expected), got, symbol)
        # передний зонд (его флаги) не тронут — их нет в политике вовсе
        self.assertTrue(ck.verify_bytes(result.result.image).ok)
        codes = {f["code"] for f in result.findings}
        self.assertIn("CAT_DELETE_LEGALITY_WARNING", codes)

    def test_sap_delete_writes_flags(self) -> None:
        image = LO00_CAL.read_bytes()
        result = self.build(image, [{"id": "s", "kind": "sap_delete"}], no_catalyst=True)
        self.assertEqual("BUILT", result.status, result.findings)
        family = "4570LO00S"
        for symbol, expected in stages.SAP_DELETE_TARGETS.items():
            got = self.cells(result.result.image, family, symbol)
            self.assertEqual(list(expected), got, symbol)

    def test_bn00s_honest_requires_data(self) -> None:
        image = BN00_CAL.read_bytes()
        result = self.build(image, [{"id": "c", "kind": "cat_delete"},
                                    {"id": "s", "kind": "sap_delete"}],
                            no_catalyst=True)
        for kind in ("cat_delete", "sap_delete"):
            package = next(p for p in result.packages if p.id == kind)
            self.assertEqual("requires_data", package.status, kind)
            self.assertIn("не доказаны", package.detail)

    def test_v3_variant_collects_the_layer(self) -> None:
        image = BO00_CAL.read_bytes()
        result = self.build(
            image,
            [{"id": "i1", "kind": "ignition", "delta_deg": 1.5},
             {"id": "c", "kind": "cat_delete"},
             {"id": "s", "kind": "sap_delete"}],
            variant="v3", no_catalyst=True,
        )
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertEqual({"ignition", "cat_delete", "sap_delete"},
                         {p.id for p in result.written_packages})

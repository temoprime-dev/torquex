# -*- coding: utf-8 -*-
"""Вариант 1 в генераторе MS45: состав, ворота, наряд (docs/TASK_VARIANT1_RU.md).

MS45 отличается от MS43 калибровочным путём (без патчей кода): launch control
остаётся «недоступно», «off»-опции (DISA/ISA/вентилятор) ждут данных клиента.
Здесь проверяются ворота варианта 1, правовая справка, профиль топлива и блок
откатa на сток с проверкой по манифесту.
"""

from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MS45.tools import policy as pl  # noqa: E402
from MS45.tools import stages  # noqa: E402
from tools import pack_log_fixtures as fx  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BO00_CAL = ROOT / "MS45" / "firmware" / "MS45.0_456BO00S_E83" / "7561448_0044560BO00S0CO0R.bin"

#: Синтетические логи (реальных логов MS4x у владельца нет — см. docs/TASK_LOGS_RU.md).
FIXTURE_DIR = Path(tempfile.mkdtemp(prefix="ms45_zone_log_"))


class Variant1Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.image = BO00_CAL.read_bytes()
        cls.policy = pl.load_policy()

    def build(self, intents: list[dict], **extra) -> stages.StageResult:
        request = {"stage": "stage1", "variant": "v1", "customer": "тест",
                   "intents": list(intents), **extra}
        return stages.build("stage1", self.image, request, policy=self.policy)

    @staticmethod
    def codes(result: stages.StageResult) -> set[str]:
        return {item["code"] for item in result.findings}


class OctaneProfileTests(Variant1Base):
    def test_ron98_profile_is_reported(self) -> None:
        result = self.build(
            [{"kind": "octane", "fuel": "ron98"},
             {"kind": "ignition", "delta_deg": 1.5}])
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertEqual("ron98", result.manifest["octane"]["profile"])
        self.assertIn("топливо: RON98", stages.build_note(result, BO00_CAL.name))

    def test_ron91_forbids_ignition_addition(self) -> None:
        result = self.build(
            [{"kind": "octane", "fuel": "ron91"},
             {"kind": "ignition", "delta_deg": 0.75}])
        self.assertEqual("REJECTED", result.status)
        self.assertIn("OCTANE_PROFILE_NO_ADDITION", self.codes(result))

    def test_fuel_by_log_stays_out_of_variant_one(self) -> None:
        """Топливо по логу — вариант 2; в базовом варианте его нет."""
        result = self.build([{"kind": "fuel", "ti_fl_enrich_lambda": 0.02}])
        self.assertEqual("REJECTED", result.status)
        self.assertIn("VARIANT_SCOPE_VIOLATION", self.codes(result))


class LegalityGateTests(Variant1Base):
    POPS = {"kind": "pops", "delay_s": 0.5, "profile": "light"}

    def test_pops_without_legality_is_rejected(self) -> None:
        result = self.build([self.POPS], no_catalyst=True,
                            acknowledge_temperature=True)
        self.assertEqual("REJECTED", result.status)
        self.assertIn("ACKNOWLEDGE_LEGALITY_REQUIRED", self.codes(result))

    def test_pops_with_legality_builds_and_gets_notice(self) -> None:
        result = self.build([self.POPS], acknowledge_legality=True, no_catalyst=True,
                            acknowledge_temperature=True)
        self.assertEqual("BUILT", result.status, result.findings)
        notice = result.manifest["legal_notice"]
        self.assertIn("715/2007", " ".join(notice["text_ru"]))
        self.assertIn("1068.101(b)", " ".join(notice["text_en"]))
        note = stages.build_note(result, BO00_CAL.name)
        self.assertIn("## Правовая справка", note)

    def test_cat_delete_requires_no_catalyst_after_legality(self) -> None:
        result = self.build([{"kind": "cat_delete"}], acknowledge_legality=True)
        self.assertEqual("REJECTED", result.status)
        self.assertIn("CAT_DELETE_NO_CATALYST_REQUIRED", self.codes(result))


class OptionTests(Variant1Base):
    """Опции варианта 1 для MS45: запрос не ломает заявку, но ждёт данных."""

    def test_options_wait_for_data_with_catalog_reasons(self) -> None:
        result = self.build([{"kind": "disa_off"}, {"kind": "isa_off"},
                             {"kind": "fan_thermostat"}])
        self.assertEqual("REQUIRES_DATA", result.status, result.findings)
        block = {item["id"]: item for item in result.manifest["requires_data"]}
        self.assertEqual({"disa_off", "isa_off", "fan_thermostat"}, set(block))
        for package, item in block.items():
            self.assertEqual(f"{package.upper()}_REQUIRES_DATA", item["code"])
            self.assertGreater(len(item["why"]), 40, package)
            self.assertGreaterEqual(len(item["needs"]), 3, package)

    def test_launch_control_stays_unavailable(self) -> None:
        result = self.build([{"kind": "launch_control"}])
        self.assertEqual("REQUIRES_DATA", result.status, result.findings)
        pending = result.pending[0]
        self.assertEqual("launch_control", pending.id)
        self.assertIn("патч", pending.detail.lower())

    def test_options_do_not_block_ignition(self) -> None:
        result = self.build([{"kind": "isa_off"},
                             {"kind": "ignition", "delta_deg": 1.0}])
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertEqual({"ignition"}, {item.id for item in result.written_packages})


class ZoneLayoutTests(Variant1Base):
    """Раскладка добавки по полосам нагрузки (docs/PACKS_LOG_REQUIREMENTS_RU.md §7.2).

    Ось оборотов карт ``__n__maf`` в MS45 не доказана (``find_maf_axis_ms45``
    находит только ось нагрузки), поэтому предел берётся по полосе нагрузки —
    минимум по зонам полосы. Полоса под запретом лога не получает добавку,
    полоса без данных — только консервативный потолок без лога (+1.5°).
    """

    VARIANT = "v2"
    SYMBOL = "ip_iga_bas_ron_98__n__maf"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.clean = fx.evidence_from_csv(
            fx.write_ignition_csv(FIXTURE_DIR / "ignition_clean.csv"), "ignition")
        cls.family_policy = cls.policy.for_family("4560BO00S")
        # файл 456BO00S — уже область калибровки, чтение идёт со смещением 0
        cls.cal_offset = 0
        cls.axis = stages._policy_axis(
            cls.image, cls.family_policy, cls.cal_offset, stages.IGN_LOAD_AXIS)

    def build(self, intents: list[dict], **extra) -> stages.StageResult:
        return super().build(intents, variant=self.VARIANT, **extra)

    def package(self, result: stages.StageResult, package: str) -> stages.PackageResult:
        return next(item for item in result.packages if item.id == package)

    def synthetic_evidence(self, zones: list[dict]) -> dict:
        recommendation = {
            "kind": "pack_recommendation",
            "package": "ignition",
            "analyzer_version": stages.pack_log_analyzer.ANALYZER_VERSION,
            "confidence": "high",
            "source_sha256": "a" * 64,
            "zones": zones,
        }
        return {
            "kind": "pack_log",
            "analyzer_version": recommendation["analyzer_version"],
            "source_sha256": recommendation["source_sha256"],
            "pack_recommendation": recommendation,
        }

    # --- предел полосы (юнит) --------------------------------------------

    def test_load_band_limits(self) -> None:
        """Минимум по зонам полосы; нет зон — консервативный потолок без лога."""
        zones = [{"index": {"rpm": 2, "load": 1}, "delta_deg": 1.0},
                 {"index": {"rpm": 3, "load": 1}, "delta_deg": 3.0},
                 {"index": {"rpm": 1, "load": 2}, "delta_deg": 0.0}]
        findings: list[dict] = []
        limits, note = stages._ignition_zone_limits(
            self.synthetic_evidence(zones), [180.0, 400.0, 600.0, 800.0], 2.0, 1.5,
            findings)
        # 180 — полоса без данных; 400 — минимум (1.0) по двум зонам полосы;
        # 600 — полоса под запретом (детонация в зоне); 800 — вне сетки зон
        self.assertEqual([1.5, 1.0, 0.0, 1.5], limits)
        self.assertIn("ось оборотов карты не доказана", note)
        self.assertIn("1 столбцов в полосах без данных", note)
        self.assertIn("1 вне сетки зон", note)
        self.assertEqual([], findings)

    def test_untrusted_recommendation_is_not_used(self) -> None:
        evidence = self.synthetic_evidence(
            [{"index": {"rpm": 2, "load": 1}, "delta_deg": 3.0}])
        evidence["pack_recommendation"]["package"] = "fuel"
        findings: list[dict] = []
        limits, note = stages._ignition_zone_limits(evidence, [400.0], 2.0, 1.5, findings)
        self.assertIsNone(limits)
        self.assertIsNone(note)
        self.assertIn("рекомендация для пакета", findings[0]["message"])

    # --- раскладка в сборке (e2e) ----------------------------------------

    def test_zones_limit_high_load_bands(self) -> None:
        result = self.build([{"kind": "ignition", "delta_deg": 2.0,
                              "evidence": self.clean}])
        self.assertEqual("BUILT", result.status, result.findings)
        detail = self.package(result, "ignition").detail
        self.assertIn("раскладка по полосам нагрузки", detail)
        entry = self.family_policy.objects[self.SYMBOL]
        before = stages._read_all_ms45(self.image, entry, self.cal_offset)
        after = stages._read_all_ms45(result.result.image, entry, self.cal_offset)
        deltas = [a - b for b, a in zip(before, after)]
        quantum = entry.scale_a
        self.assertLessEqual(max(deltas), 2.0 + quantum / 2 + 1e-9)
        # столбцы вне сетки зон (нагрузка ≥ 700 мг/такт) — консервативный +1.5°
        outside = [index for index, load in enumerate(self.axis) if load >= 700.0]
        self.assertTrue(outside)
        for index in outside:
            for row in range(entry.rows):
                delta = deltas[row * entry.columns + index]
                self.assertLessEqual(delta, 1.5 + quantum, f"столбец {index}, строка {row}")
        # полосы с данными (load 300..700 мг/такт) поднимаются выше пола без лога
        self.assertTrue(
            any(value > 1.5 + 1e-9 for value in deltas),
            f"полосы с данными не поднялись выше консервативного пола: "
            f"{sorted(set(round(v, 3) for v in deltas))}")

    def test_evidence_without_recommendation_keeps_flat_delta(self) -> None:
        plain = copy.deepcopy(self.clean)
        plain.pop("pack_recommendation", None)
        result = self.build([{"kind": "ignition", "delta_deg": 2.0, "evidence": plain}])
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertIn("ZONES_UNAVAILABLE", self.codes(result))
        messages = " ".join(item["message"] for item in result.findings)
        self.assertIn("раскладка по полосам нагрузки не применена", messages)

    def test_tampered_recommendation_is_refused(self) -> None:
        broken = copy.deepcopy(self.clean)
        broken["pack_recommendation"]["source_sha256"] = "0" * 64
        result = self.build([{"kind": "ignition", "delta_deg": 2.0, "evidence": broken}])
        self.assertEqual("REQUIRES_DATA", result.status, result.findings)
        self.assertIn("EVIDENCE_TAMPERED", self.package(result, "ignition").detail)


class ZoneLayoutBase(Variant1Base):
    """Общая база раскладки топлива/фаз: образ и политика 4570LO00S (v2).

    Топливо полной нагрузки и ВАНос выпускаются с варианта 2, а ворота
    диагностики богатой смеси доказаны только у 4570LO00S.
    """

    VARIANT = "v2"
    LO00_CAL = (ROOT / "MS45" / "firmware_spdaten" / "cal"
                / "MS45.1_0044570LO00S01J2R_P7561517_cal_116K.bin")

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.image = cls.LO00_CAL.read_bytes()
        cls.policy = pl.load_policy()
        cls.family_policy = cls.policy.for_family("4570LO00S")
        cls.cal_offset = 0  # файл уже область калибровки

    def build(self, intents: list[dict], **extra) -> stages.StageResult:
        return super().build(intents, variant=self.VARIANT, **extra)

    def package(self, result: stages.StageResult, package: str) -> stages.PackageResult:
        return next(item for item in result.packages if item.id == package)

    def cells(self, image: bytes, symbol: str) -> list[float]:
        entry = self.family_policy.objects[symbol]
        return stages._read_all_ms45(image, entry, self.cal_offset)

    def axis(self, symbol: str) -> list[float]:
        values = stages._policy_axis(self.image, self.family_policy, self.cal_offset, symbol)
        self.assertIsNotNone(values, symbol)
        return values


class FuelZoneLayoutTests(ZoneLayoutBase):
    """Обогащение ``ip_lamb_fl__n`` раскладывается по столбцам оборотов.

    Ряд карты индексирован оборотами (ось из определения семейства), предел
    столбца — минимум запаса λ по зонам лога с этими оборотами; столбец без
    данных не обогащается (у топлива «нет данных» = «нельзя»).
    """

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.clean = fx.evidence_from_csv(
            fx.write_fuel_csv(FIXTURE_DIR / "lo00_fuel_clean.csv"), "fuel")

    def floor(self) -> float:
        guard_entry = self.family_policy.objects.get("c_lamb_min_ls_act_test_afr")
        guard = None if guard_entry is None else stages._current(
            self.image, guard_entry, self.cal_offset)
        return max(stages.FUEL_LAMBDA_FLOOR, guard if guard is not None else 0.0)

    def test_enrichment_is_laid_out_by_rpm(self) -> None:
        result = self.build([{"id": "f1", "kind": "fuel", "ti_fl_enrich_lambda": 0.03,
                              "evidence": self.clean}])
        self.assertEqual("BUILT", result.status, result.findings)
        detail = self.package(result, "fuel").detail
        self.assertIn("раскладка обогащения по оборотам", detail)
        before = self.cells(self.image, "ip_lamb_fl__n")
        after = self.cells(result.result.image, "ip_lamb_fl__n")
        axis = self.axis(stages.FUEL_RPM_AXIS)
        limits = stages.pack_recommendation.full_load_delta_by_rpm(
            self.clean["pack_recommendation"], axis, self.floor(), 0.03)
        entry = self.family_policy.objects["ip_lamb_fl__n"]
        quantum = entry.scale_a
        self.assertTrue(any(limit > 0 for limit in limits), "лог не дал ни одного столбца")
        self.assertTrue(any(limit <= 0 for limit in limits), "нет столбца без данных")
        for index, (b, a, limit) in enumerate(zip(before, after, limits)):
            self.assertLessEqual(a, b + 1e-9, f"обороты {axis[index]:g}")
            if limit <= 0:
                self.assertEqual(b, a, f"обороты {axis[index]:g}: без данных, а ячейка изменена")
            else:
                self.assertGreaterEqual(b - a, min(limit, 1e-9), f"обороты {axis[index]:g}")
                self.assertLessEqual(b - a, limit + quantum + 1e-9)

    def test_without_recommendation_the_enrichment_is_flat(self) -> None:
        plain = copy.deepcopy(self.clean)
        plain.pop("pack_recommendation", None)
        result = self.build([{"id": "f1", "kind": "fuel", "ti_fl_enrich_lambda": 0.03,
                              "evidence": plain}])
        self.assertEqual("BUILT", result.status, result.findings)
        detail = self.package(result, "fuel").detail
        self.assertNotIn("раскладка обогащения", detail)
        self.assertIn("ZONES_UNAVAILABLE", self.codes(result))
        messages = " ".join(item["message"] for item in result.findings)
        self.assertIn("обогащение плоское", messages)


class VanosZoneLayoutTests(ZoneLayoutBase):
    """Сдвиг фаз раскладывается по зонам (обороты × нагрузка) лога фаз."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.clean = fx.evidence_from_csv(
            fx.write_vanos_csv(FIXTURE_DIR / "lo00_vanos_clean.csv"), "vanos")

    def test_shift_only_where_the_log_confirms_it(self) -> None:
        result = self.build([{"id": "v1", "kind": "vanos", "shift_in_deg": 2.0,
                              "evidence": self.clean}])
        self.assertEqual("BUILT", result.status, result.findings)
        detail = self.package(result, "vanos").detail
        self.assertIn("раскладка сдвига по зонам лога", detail)
        symbol = "ip_cam_sp_2_in_pl__n__maf_ivvt"
        before = self.cells(self.image, symbol)
        after = self.cells(result.result.image, symbol)
        rpm_axis = self.axis(stages.VANOS_RPM_AXIS)
        load_axis = self.axis(stages.VANOS_LOAD_AXIS)
        limits = stages.pack_recommendation.shift_limits_for_map(
            self.clean["pack_recommendation"], rpm_axis, load_axis, 2.0)
        entry = self.family_policy.objects[symbol]
        quantum = abs(entry.scale_a)
        self.assertGreater(sum(1 for value in limits if value > 0), 0)
        for index, (b, a) in enumerate(zip(before, after)):
            if limits[index] <= 0:
                self.assertEqual(b, a, f"ячейка {index}: зона без данных, а сдвиг записан")
            else:
                self.assertGreater(a, b, f"ячейка {index}")
                self.assertLessEqual(a - b, 2.0 + quantum + 1e-9, f"ячейка {index}")
        # строки ниже 2000 об/мин (вне сетки зон) не двигаются
        for row, rpm in enumerate(rpm_axis):
            if rpm >= 2000:
                continue
            for column in range(entry.columns):
                index = row * entry.columns + column
                self.assertEqual(before[index], after[index], f"{rpm:g} об/мин")

    def test_without_recommendation_the_shift_is_flat(self) -> None:
        plain = copy.deepcopy(self.clean)
        plain.pop("pack_recommendation", None)
        result = self.build([{"id": "v1", "kind": "vanos", "shift_in_deg": 2.0,
                              "evidence": plain}])
        self.assertEqual("BUILT", result.status, result.findings)
        detail = self.package(result, "vanos").detail
        self.assertNotIn("раскладка сдвига", detail)
        self.assertIn("ZONES_UNAVAILABLE", self.codes(result))
        messages = " ".join(item["message"] for item in result.findings)
        self.assertIn("сдвиг плоский", messages)


class RollbackTests(Variant1Base):
    def test_manifest_carries_stock_hash_and_verify_command(self) -> None:
        result = self.build([{"kind": "ignition", "delta_deg": 1.0}])
        rollback = result.manifest["rollback"]
        self.assertEqual(hashlib.sha256(self.image).hexdigest(),
                         rollback["stock_sha256"])
        self.assertIn("--verify-manifest", rollback["verify_command"])
        note = stages.build_note(result, BO00_CAL.name)
        self.assertIn("## Откат на сток", note)
        self.assertIn("--verify-manifest", note)


if __name__ == "__main__":
    unittest.main()

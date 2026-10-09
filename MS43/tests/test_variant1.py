# -*- coding: utf-8 -*-
"""Вариант 1 в генераторе MS43: состав, ворота, наряд (docs/TASK_VARIANT1_RU.md).

Проверяется то, что задание §1/§2/§5.3 требует именно от генератора:

* профиль топлива (октан) — RON91/RON95 не получают добавку зажигания;
* правовая справка обязательна для хлопков и слоя диагностики;
* новые одобренные намерения (DISA off, ISA off, холостой ход,
  вентилятор/термостат) не отказ заявке, а честный ``REQUIRES_DATA``;
* наряд и манифест несут правовую справку и блок откатa на сток (sha256).
"""

from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MS43.tools import stages  # noqa: E402
from MS43.tools.policy import load_policy  # noqa: E402
from tools import pack_log_fixtures as fx  # noqa: E402
from tools import pack_recommendation  # noqa: E402
from tools.pack_recommendation import zone_delta  # noqa: E402

REFERENCE = (Path(__file__).resolve().parents[1] / "firmware"
             / "Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin")

#: Синтетические логи (реальных логов MS4x у владельца нет — см. docs/TASK_LOGS_RU.md).
FIXTURE_DIR = Path(tempfile.mkdtemp(prefix="ms43_zone_log_"))

POPCORN = {
    "id": "p1", "kind": "popcorn", "band": "light",
    "no_catalyst": "катализатор удалён физически, машина вне дорог общего пользования",
    "thermal_watch": True,
    "road_validation_plan": "первые заезды: 2-я и 3-я передачи, следим за ОЖ и выпуском",
}
RPM_LIMIT = {
    "id": "r1", "kind": "rpm_limit", "transmission": "manual",
    "soft_rpm": 6688, "hard_rpm": 6720,
    "acknowledge_risk": True, "acknowledge_oil_pump": True,
}


class Variant1Base(unittest.TestCase):
    #: Вариант поставки по умолчанию; классы раскладки по зонам берут ``v2``.
    VARIANT = "v1"

    @classmethod
    def setUpClass(cls) -> None:
        cls.image = REFERENCE.read_bytes()
        cls.policy = load_policy()

    def build(self, intents: list[dict], **extra) -> stages.StageResult:
        request = {"stage": "stage1", "variant": self.VARIANT, "customer": "тест",
                   "intents": list(intents), **extra}
        return stages.build("stage1", self.image, request, policy=self.policy)

    @staticmethod
    def codes(result: stages.StageResult) -> set[str]:
        return {item["code"] for item in result.findings}

    def synthetic_evidence(self, zones: list[dict], package: str = "ignition",
                           limits: dict | None = None) -> dict:
        """Доказательство с рекомендацией — для проверки раскладки без фикстур."""
        recommendation = {
            "kind": "pack_recommendation",
            "package": package,
            "limits": limits or {},
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


class OctaneProfileTests(Variant1Base):
    """Профиль топлива: клиент выбирает, добавку получает только RON98."""

    def test_ron98_profile_is_reported(self) -> None:
        result = self.build(
            [{"id": "o1", "kind": "octane", "fuel": "ron98"},
             {"id": "i1", "kind": "ignition", "delta_deg": 1.5}])
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertEqual("ron98", result.octane_profile)
        self.assertEqual("ron98", result.manifest["octane"]["profile"])
        note = stages.build_note(result, REFERENCE.name)
        self.assertIn("топливо: RON98", note)

    def test_ron95_has_smaller_stage1_cap_than_ron98(self) -> None:
        """RON95 в Stage 1 — до +1,5° (не до 3,0°, как RON98: запись идёт в
        карту RON98, а ЭБУ при выученном 95-м отрабатывает только ~57% угла)."""
        result = self.build(
            [{"id": "o1", "kind": "octane", "fuel": "ron95"},
             {"id": "i1", "kind": "ignition", "delta_deg": 1.5}])
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertEqual({"ignition"}, {item.id for item in result.written_packages})
        result = self.build(
            [{"id": "o1", "kind": "octane", "fuel": "ron95"},
             {"id": "i1", "kind": "ignition", "delta_deg": 1.6}])
        self.assertEqual("REJECTED", result.status)
        self.assertIn("VARIANT_SCOPE_VIOLATION", self.codes(result))

    def test_ron91_forbids_ignition_addition(self) -> None:
        result = self.build(
            [{"id": "o1", "kind": "octane", "fuel": "ron91"},
             {"id": "i1", "kind": "ignition", "delta_deg": 0.75}])
        self.assertEqual("REJECTED", result.status)
        self.assertIn("OCTANE_PROFILE_NO_ADDITION", self.codes(result))
        self.assertIsNone(result.plan)
        note = stages.build_note(result, REFERENCE.name)
        self.assertIn("OCTANE_PROFILE_NO_ADDITION", note)

    def test_ron95_keeps_base_fuel_addition(self) -> None:
        """Профиль RON95 ограничивает только зажигание, топливо не трогает."""
        result = self.build(
            [{"id": "o1", "kind": "octane", "fuel": "ron95"},
             {"id": "f1", "kind": "fuel", "tib_add_ms": 0.1, "tib_add_percent": 4.0}])
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertEqual({"fuel"}, {item.id for item in result.written_packages})

    def test_conflicting_profile_is_rejected(self) -> None:
        result = self.build([{"id": "o1", "kind": "octane", "fuel": "ron98"}],
                            octane="ron95")
        self.assertEqual("REJECTED", result.status)
        self.assertIn("OCTANE_PROFILE_CONFLICT", self.codes(result))

    def test_profile_is_optional_for_old_requests(self) -> None:
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 1.5}])
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertIsNone(result.octane_profile)
        self.assertIn("не выбран", result.manifest["octane"]["note_ru"])


class LegalityGateTests(Variant1Base):
    """Правовая справка (задание §2): без подтверждения не выпускается."""

    def test_popcorn_without_acknowledgement_is_rejected(self) -> None:
        result = self.build([POPCORN])
        self.assertEqual("REJECTED", result.status)
        self.assertIn("ACKNOWLEDGE_LEGALITY_REQUIRED", self.codes(result))

    def test_popcorn_with_acknowledgement_builds_and_gets_notice(self) -> None:
        result = self.build([POPCORN], acknowledge_legality=True)
        self.assertEqual("BUILT", result.status, result.findings)
        notice = result.manifest["legal_notice"]
        self.assertIsNotNone(notice)
        self.assertIn("715/2007", " ".join(notice["text_ru"]))
        self.assertIn("203(a)(3)", " ".join(notice["text_en"]))
        note = stages.build_note(result, REFERENCE.name)
        self.assertIn("## Правовая справка", note)
        self.assertIn("Legal notice (EN)", note)

    def test_dtc_layer_needs_legality_and_no_catalyst(self) -> None:
        intent = {"id": "d1", "kind": "dtc", "groups": ["cat"]}
        result = self.build([intent], no_catalyst=True)
        self.assertEqual("REJECTED", result.status)
        self.assertIn("ACKNOWLEDGE_LEGALITY_REQUIRED", self.codes(result))
        result = self.build([intent], acknowledge_legality=True)
        self.assertEqual("REJECTED", result.status)
        self.assertIn("DTC_NO_CATALYST_REQUIRED", self.codes(result))
        result = self.build([intent], acknowledge_legality=True, no_catalyst=True)
        self.assertEqual("BUILT", result.status, result.findings)

    def test_manifest_has_no_notice_when_not_needed(self) -> None:
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 1.0}])
        self.assertIsNone(result.manifest["legal_notice"])


class ApprovedIntentTests(Variant1Base):
    """Одобренные функции варианта 1 — отдельными намерениями."""

    def test_limits_are_written_under_v1(self) -> None:
        result = self.build([RPM_LIMIT,
                             {"id": "v1", "kind": "vmax", "transmission": "manual",
                              "value_kmh": 250, "hard_add_kmh": 5.0,
                              "acknowledge_risk": True}])
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertIn("limits", {item.id for item in result.written_packages})

    def test_vanos_stays_out_of_variant_one(self) -> None:
        """ВАНос — расширение (вариант 2), в базовом варианте его нет."""
        result = self.build([{"id": "vn1", "kind": "vanos", "shift_in_deg": 2.0}])
        self.assertEqual("REJECTED", result.status)
        self.assertIn("VARIANT_SCOPE_VIOLATION", self.codes(result))

    def test_options_wait_for_data_with_concrete_needs(self) -> None:
        result = self.build([
            {"id": "d1", "kind": "disa_off"},
            {"id": "s1", "kind": "isa_off"},
            {"id": "i1", "kind": "idle"},
            {"id": "f1", "kind": "fan_thermostat"},
        ])
        self.assertEqual("REQUIRES_DATA", result.status, result.findings)
        pending = {item.id: item for item in result.pending}
        self.assertEqual({"disa_off", "isa_off", "idle", "fan_thermostat"},
                         set(pending))
        for package, item in pending.items():
            self.assertGreaterEqual(len(item.needs), 3, package)
            self.assertTrue(item.detail, package)

    def test_options_do_not_block_other_packages(self) -> None:
        result = self.build(
            [{"id": "s1", "kind": "isa_off"},
             {"id": "i1", "kind": "ignition", "delta_deg": 1.5}])
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertEqual({"ignition"}, {item.id for item in result.written_packages})
        self.assertEqual(["isa_off"], [item.id for item in result.pending])


class ZoneLayoutTests(Variant1Base):
    """Раскладка добавки по зонам лога (docs/PACKS_LOG_REQUIREMENTS_RU.md §7.2).

    Рекомендация анализатора — не «красивая картинка»: генератор берёт из неё
    форму добавки по зонам (обороты × нагрузка), а заявка задаёт потолок. Зона
    с детонацией/перегревом/бедной смесью получает 0, зона без данных —
    консервативный потолок без лога (+1.5°), а не запрошенные 2.5–3°.
    Добавки по логам — состав варианта v2 (в v1 потолок 1.5° без лога).
    """

    VARIANT = "v2"
    SYMBOL = "ip_iga_ron_98_pl_ivvt__n__maf"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.clean = fx.evidence_from_csv(
            fx.write_ignition_csv(FIXTURE_DIR / "ignition_clean.csv"), "ignition")

    def build(self, intents: list[dict], **extra) -> stages.StageResult:
        return super().build(intents, variant=self.VARIANT, **extra)

    def package(self, result: stages.StageResult, package: str) -> stages.PackageResult:
        return next(item for item in result.packages if item.id == package)

    def cells(self, image: bytes, symbol: str) -> list[float]:
        entry = self.policy.objects[symbol]
        return [stages._current(image, entry, index)
                for index in range(entry.rows * entry.columns)]

    def written_deltas(self, result: stages.StageResult) -> list[float]:
        before = self.cells(self.image, self.SYMBOL)
        after = self.cells(result.result.image, self.SYMBOL)
        return [a - b for b, a in zip(before, after)]

    # --- раскладка по ячейкам (юнит) -------------------------------------

    def test_zones_are_applied_cell_by_cell(self) -> None:
        zones = [{"index": {"rpm": 1, "load": 2}, "delta_deg": 3.0},
                 {"index": {"rpm": 2, "load": 2}, "delta_deg": 0.0}]
        entry = self.policy.objects[self.SYMBOL]
        findings: list[dict] = []
        values, note = stages._ignition_zone_caps(
            self.synthetic_evidence(zones), self.policy, self.image, entry,
            2.5, 1.5, findings)
        self.assertIsNotNone(values)
        self.assertEqual(entry.rows * entry.columns, len(values))
        # 2.5 — потолок заявки; 1.5 — зона без данных; 0 — зона под запретом
        self.assertEqual({0.0, 1.5, 2.5}, {round(value, 3) for value in values})
        rpm_axis, load_axis = stages._ignition_axes(self.image, self.policy)
        rec = self.synthetic_evidence(zones)["pack_recommendation"]
        for zone, expected in ((zones[0], 2.5), (zones[1], 0.0)):
            wanted = sum(
                1 for rpm in rpm_axis for load in load_axis
                if zone_delta(rec, rpm, load, None) == zone["delta_deg"])
            got = sum(1 for value in values if value == expected)
            self.assertEqual(wanted, got, zone["index"])
            self.assertGreater(wanted, 0, "зона не покрывает ни одной ячейки")
        self.assertIn("пределы по зонам лога", note)
        self.assertIn("детонация/перегрев/бедная смесь", note)
        self.assertEqual([], findings)  # рекомендация принята, находок нет

    def test_missing_axes_fall_back_to_flat(self) -> None:
        entry = self.policy.objects[self.SYMBOL]
        findings: list[dict] = []
        fake_policy = types.SimpleNamespace(objects={})
        values, note = stages._ignition_zone_caps(
            self.synthetic_evidence([{"index": {"rpm": 1, "load": 2}, "delta_deg": 3.0}]),
            fake_policy, self.image, entry, 2.5, 1.5, findings)
        self.assertIsNone(values)
        self.assertIsNone(note)
        self.assertEqual(["ZONES_UNAVAILABLE"], [item["code"] for item in findings])

    def test_untrusted_recommendation_is_not_used(self) -> None:
        """Чужая рекомендация (другой sha/версия) в запись не идёт."""
        evidence = self.synthetic_evidence(
            [{"index": {"rpm": 1, "load": 2}, "delta_deg": 3.0}])
        evidence["pack_recommendation"]["source_sha256"] = "b" * 64
        entry = self.policy.objects[self.SYMBOL]
        findings: list[dict] = []
        values, _ = stages._ignition_zone_caps(
            evidence, self.policy, self.image, entry, 2.5, 1.5, findings)
        self.assertIsNone(values)
        self.assertIn("не привязана к sha256", findings[0]["message"])

    # --- раскладка в сборке (e2e) ----------------------------------------

    def test_add_without_log_has_no_zone_layout(self) -> None:
        """До 1.5° лог не нужен — раскладки нет, лишних находок тоже."""
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 1.5}])
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertNotIn("ZONES_UNAVAILABLE", self.codes(result))
        detail = self.package(result, "ignition").detail
        self.assertNotIn("раскладка по зонам", detail)

    def test_log_zones_shape_the_addition(self) -> None:
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 2.5,
                              "evidence": self.clean}])
        self.assertEqual("BUILT", result.status, result.findings)
        detail = self.package(result, "ignition").detail
        self.assertIn("пределы по зонам лога", detail)
        confidence = self.clean["pack_recommendation"]["confidence"]
        self.assertIn(confidence, detail)
        self.assertEqual([], result.findings)
        # потолок заявки соблюдён, а часть ячеек осталась на консервативном полу
        deltas = self.written_deltas(result)
        self.assertLessEqual(max(deltas), 2.5 + 0.375 / 2 + 1e-9)
        self.assertTrue(any(0 < value <= 1.5 + 0.375 / 2 + 1e-9 for value in deltas),
                        "лог покрыл все ячейки — консервативный пол не виден")

    def test_evidence_without_recommendation_keeps_flat_delta(self) -> None:
        plain = copy.deepcopy(self.clean)
        plain.pop("pack_recommendation", None)
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 2.5,
                              "evidence": plain}])
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertIn("ZONES_UNAVAILABLE", self.codes(result))
        messages = " ".join(item["message"] for item in result.findings)
        self.assertIn("потолок плоский", messages)

    def test_tampered_recommendation_is_refused(self) -> None:
        """Числа рекомендации руками не правятся: ворота увидят расхождение."""
        broken = copy.deepcopy(self.clean)
        broken["pack_recommendation"]["source_sha256"] = "0" * 64
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 2.5,
                              "evidence": broken}])
        self.assertEqual("REQUIRES_DATA", result.status, result.findings)
        self.assertIn("EVIDENCE_TAMPERED",
                      self.package(result, "ignition").detail)


class FuelZoneLayoutTests(Variant1Base):
    """Обогащение полной нагрузки раскладывается по оборотам (зоны лога λ).

    Карта ``ip_ti_fl__n`` индексирована оборотами: правка берётся по минимуму
    запаса λ среди зон с данными при этих оборотах, а обороты без данных не
    обогащаются вовсе (в отличие от зажигания, у топлива «нет данных» = нельзя).
    """

    VARIANT = "v2"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.clean = fx.evidence_from_csv(
            fx.write_fuel_csv(FIXTURE_DIR / "fuel_clean.csv"), "fuel")

    def cells(self, image: bytes, symbol: str) -> list[float]:
        entry = self.policy.objects[symbol]
        return [stages._current(image, entry, index)
                for index in range(entry.rows * entry.columns)]

    def axis(self) -> list[float]:
        return stages._policy_axis(self.image, self.policy, "ldpm_n_11")

    def test_flat_enrichment_uses_the_full_request(self) -> None:
        """Контроль: без рекомендации в логе обогащение плоское (древний путь)."""
        plain = copy.deepcopy(self.clean)
        plain.pop("pack_recommendation", None)
        result = self.build([{"id": "f1", "kind": "fuel", "ti_fl_enrich_lambda": 0.05,
                              "evidence": plain}])
        self.assertEqual("BUILT", result.status, result.findings)
        package = next(p for p in result.packages if p.id == "fuel")
        self.assertNotIn("раскладка обогащения", package.detail)
        self.assertIn("ZONES_UNAVAILABLE", self.codes(result))
        messages = " ".join(item["message"] for item in result.findings)
        self.assertIn("обогащение плоское", messages)
        before = self.cells(self.image, "ip_ti_fl__n")
        after = self.cells(result.result.image, "ip_ti_fl__n")
        changed = [a - b for b, a in zip(before, after) if a != b]
        # плоское обогащение: карта затронута целиком (кроме ячейки на полу λ)
        self.assertGreaterEqual(len(changed), len(before) - 1)
        self.assertGreater(max(changed), 0.04)  # почти заявленные 0.05 λ

    def test_zone_margins_limit_the_enrichment(self) -> None:
        result = self.build([{"id": "f1", "kind": "fuel", "ti_fl_enrich_lambda": 0.05,
                              "evidence": self.clean}])
        self.assertEqual("BUILT", result.status, result.findings)
        package = next(p for p in result.packages if p.id == "fuel")
        self.assertIn("раскладка обогащения по оборотам", package.detail)
        entry = self.policy.objects["ip_ti_fl__n"]
        before = self.cells(self.image, "ip_ti_fl__n")
        after = self.cells(result.result.image, "ip_ti_fl__n")
        axis = self.axis()
        rec = self.clean["pack_recommendation"]
        limits = pack_recommendation.full_load_delta_by_rpm(
            rec, axis, floor=0.82, requested=0.05)
        for index, (b, a, limit) in enumerate(zip(before, after, limits)):
            if limit <= 0:
                self.assertEqual(b, a, f"обороты {axis[index]:g}: без данных, а ячейка изменена")
            else:
                self.assertGreaterEqual(a, b)
                self.assertLessEqual(a - b, limit + entry.scale_a, f"обороты {axis[index]:g}")
        # обороты ниже 2000 (вне сетки зон) обязаны остаться стоковыми
        for index, rpm in enumerate(axis):
            if rpm >= 2000:
                continue
            self.assertEqual(before[index], after[index], f"обороты {rpm:g}")

class VanosZoneLayoutTests(Variant1Base):
    """Сдвиг фаз раскладывается по зонам (обороты × нагрузка) из лога фаз."""

    VARIANT = "v2"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.clean = fx.evidence_from_csv(
            fx.write_vanos_csv(FIXTURE_DIR / "vanos_clean.csv"), "vanos")

    def vanos_axes(self) -> tuple[list[float], list[float]]:
        return (stages._policy_axis(self.image, self.policy, "ldpm_n_5"),
                stages._policy_axis(self.image, self.policy, "ldpm_maf_ivvt_2"))

    def test_shift_is_written_only_where_log_has_data(self) -> None:
        result = self.build([{"id": "v1", "kind": "vanos", "shift_in_deg": 2.0,
                              "evidence": self.clean}])
        self.assertEqual("BUILT", result.status, result.findings)
        package = next(p for p in result.packages if p.id == "vanos")
        self.assertIn("раскладка сдвига по зонам лога", package.detail)
        symbol = "ip_cam_sp_tco_2_in_pl__n__maf_iv"
        entry = self.policy.objects[symbol]
        before = [stages._current(self.image, entry, i)
                  for i in range(entry.rows * entry.columns)]
        after = [stages._current(result.result.image, entry, i)
                 for i in range(entry.rows * entry.columns)]
        rpm_axis, load_axis = self.vanos_axes()
        limits = pack_recommendation.shift_limits_for_map(
            self.clean["pack_recommendation"], rpm_axis, load_axis, 2.0)
        self.assertGreater(sum(1 for v in limits if v > 0), 0)
        quantum = entry.scale_a
        for index, (b, a) in enumerate(zip(before, after)):
            if limits[index] <= 0:
                self.assertEqual(b, a, f"ячейка {index}: зона без данных, а сдвиг записан")
            else:
                self.assertGreater(a, b, f"ячейка {index}")
                self.assertLessEqual(a - b, round(2.0 / quantum) * quantum + 1e-9)
        # строки ниже 2000 об/мин (вне сетки зон) не двигаются
        for row, rpm in enumerate(rpm_axis):
            if rpm >= 2000:
                continue
            for column in range(entry.columns):
                index = row * entry.columns + column
                self.assertEqual(before[index], after[index], f"{rpm:g} об/мин")

    def test_opposite_direction_is_not_shifted(self) -> None:
        """Если лог значимо указывает в другую сторону — сдвиг не подтверждён."""
        zones = [
            {"index": {"rpm": 0, "load": 1}, "shift_deg": -2.5,
             "track_error_max_deg": 0.8},
            {"index": {"rpm": 0, "load": 2}, "shift_deg": 2.5,
             "track_error_max_deg": 0.8},
        ]
        evidence = self.synthetic_evidence(zones, package="vanos",
                                          limits={"max_track_error_deg": 3.0})
        limits = pack_recommendation.shift_limits_for_map(
            evidence["pack_recommendation"], [2500.0], [400.0, 500.0], +2.0)
        self.assertEqual([0.0, 2.0], limits)  # 300–450: лог против; 450–700: за

    def test_broken_mechanics_zone_is_not_shifted(self) -> None:
        zones = [{"index": {"rpm": 0, "load": 1}, "shift_deg": 0.0,
                  "track_error_max_deg": 4.5}]
        evidence = self.synthetic_evidence(zones, package="vanos",
                                          limits={"max_track_error_deg": 3.0})
        limits = pack_recommendation.shift_limits_for_map(
            evidence["pack_recommendation"], [2500.0], [400.0], +2.0)
        self.assertEqual([0.0], limits)  # сначала ремонт, потом цели

    def test_missing_recommendation_keeps_flat_shift(self) -> None:
        plain = copy.deepcopy(self.clean)
        plain.pop("pack_recommendation", None)
        result = self.build([{"id": "v1", "kind": "vanos", "shift_in_deg": 2.0,
                              "evidence": plain}])
        self.assertEqual("BUILT", result.status, result.findings)
        self.assertIn("ZONES_UNAVAILABLE", self.codes(result))
        messages = " ".join(item["message"] for item in result.findings)
        self.assertIn("сдвиг плоский", messages)


class RollbackTests(Variant1Base):
    """Откат на сток: sha256 стока, инструкция возврата — в манифесте и наряде."""

    def test_manifest_and_note_carry_rollback(self) -> None:
        result = self.build([{"id": "i1", "kind": "ignition", "delta_deg": 1.5}])
        rollback = result.manifest["rollback"]
        self.assertEqual(hashlib.sha256(self.image).hexdigest(),
                         rollback["stock_sha256"])
        self.assertGreaterEqual(len(rollback["instructions_ru"]), 3)
        note = stages.build_note(result, REFERENCE.name)
        self.assertIn("## Откат на сток", note)
        self.assertIn(rollback["stock_sha256"], note)

    def test_rollback_is_present_even_when_rejected(self) -> None:
        result = self.build([{"id": "s1", "kind": "isa_off"}])
        self.assertEqual("REQUIRES_DATA", result.status)
        self.assertIn("stock_sha256", result.manifest["rollback"])


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Тесты анализатора-советчика (``tools/pack_recommendation.py``).

Задание `docs/TASK_VARIANT1_RU.md` §3: рекомендации выводятся из лога, не
обходят границы проекта и честно говорят «недостаточно данных». Проверки — на
синтетических фикстурах (`tools/pack_log_fixtures.py`): реальных логов MS4x у
владельца нет.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import pack_log_analyzer as analyzer  # noqa: E402
from tools import pack_log_fixtures as fx  # noqa: E402
from tools import pack_recommendation  # noqa: E402

SPEC = analyzer.load_requirements()


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def recommendation(self, path, pack, ron=None):
        table = analyzer.read_log_table(path)
        return pack_recommendation.build_recommendation(table, pack, SPEC, ron)

    # --- зажигание -------------------------------------------------------

    def test_clean_log_recommends_within_project_caps(self):
        csv_path = fx.write_ignition_csv(self.dir / "clean.csv")
        rec = self.recommendation(csv_path, "ignition", ron=98)
        self.assertEqual("high", rec["confidence"])
        self.assertTrue(rec["zones"])
        caps = SPEC["packages"]["ignition"]["gates"]
        self.assertLessEqual(
            max(zone["delta_deg"] for zone in rec["zones"]),
            float(caps["delta_with_log_deg"]))
        self.assertEqual(float(caps["delta_with_log_deg"]),
                         rec["limits"]["max_add_deg_with_retard_log"])
        self.assertEqual(float(caps["delta_without_log_deg"]),
                         rec["limits"]["max_add_deg_without_retard_log"])
        self.assertEqual([], rec["warnings"])

    def test_knock_zone_gets_zero_delta_and_warning(self):
        csv_path = fx.write_ignition_csv(self.dir / "knock.csv", knock=True)
        rec = self.recommendation(csv_path, "ignition", ron=98)
        self.assertTrue(any("детонация" in warning for warning in rec["warnings"]),
                        rec["warnings"])
        high_zone = next(zone for zone in rec["zones"]
                         if zone["zone_ru"].startswith("n 5000–6500"))
        self.assertEqual(0.0, high_zone["delta_deg"])
        self.assertIn("детонация", high_zone["recommendation_ru"])
        # Остальные зоны без детонации добавку сохраняют.
        calm = [zone for zone in rec["zones"] if zone["delta_deg"] > 0]
        self.assertTrue(calm)

    def test_octane_below_ron98_removes_addition(self):
        csv_path = fx.write_ignition_csv(self.dir / "ron95.csv")
        rec = self.recommendation(csv_path, "ignition", ron=95)
        self.assertTrue(any("RON" in warning for warning in rec["warnings"]))
        self.assertTrue(all(zone["delta_deg"] == 0.0 for zone in rec["zones"]))
        self.assertEqual("low", rec["confidence"])

    def test_hot_intake_zone_is_blocked_and_warned(self):
        csv_path = fx.write_ignition_csv(self.dir / "hot.csv", hot_iat=True)
        rec = self.recommendation(csv_path, "ignition", ron=98)
        self.assertTrue(any("впуске" in warning for warning in rec["warnings"]),
                        rec["warnings"])
        blocked = [zone for zone in rec["zones"]
                   if zone["delta_deg"] == 0.0]
        self.assertTrue(blocked)

    def test_log_without_knock_channels_uses_lower_cap(self):
        # CSV-экспорт Test0 (full=False) без канала вердикта детонации.
        csv_path = fx.write_test0_csv(self.dir / "test0_partial.csv")
        rec = self.recommendation(csv_path, "ignition", ron=98)
        caps = SPEC["packages"]["ignition"]["gates"]
        self.assertTrue(rec["zones"])
        self.assertLessEqual(
            max(zone["delta_deg"] for zone in rec["zones"]),
            float(caps["delta_without_log_deg"]))
        self.assertTrue(all(
            zone["delta_deg"] <= float(caps["delta_without_log_deg"])
            for zone in rec["zones"]))

    def test_short_log_has_no_zones_and_says_so(self):
        path = self.dir / "short.csv"
        path.write_text(
            "time;rpm;load;ignition angle;coolant;iat\n"
            + "\n".join(f"{i * 0.1:.1f};2000;400;25.0;90.0;30.0" for i in range(6))
            + "\n", encoding="utf-8", newline="\n")
        rec = self.recommendation(path, "ignition", ron=98)
        self.assertEqual("insufficient", rec["confidence"])
        self.assertEqual([], rec["zones"])

    # --- топливо ---------------------------------------------------------

    def test_fuel_log_reports_enrichment_headroom(self):
        csv_path = fx.write_fuel_csv(self.dir / "fuel.csv")
        rec = self.recommendation(csv_path, "fuel")
        floor = float(SPEC["packages"]["fuel"]["criteria"]["lambda_target_min"])
        self.assertTrue(rec["zones"])
        for zone in rec["zones"]:
            self.assertGreaterEqual(zone["lambda_min"], floor)
            self.assertGreaterEqual(zone["enrichment_factor"], 1.0)
            self.assertLessEqual(zone["enrichment_factor"], 1.05)
        self.assertEqual(floor, rec["limits"]["lambda_floor"])

    def test_lean_log_forbids_enrichment_and_warns(self):
        csv_path = fx.write_fuel_csv(self.dir / "lean.csv", lean=True)
        rec = self.recommendation(csv_path, "fuel")
        self.assertTrue(any("переобогащение" in warning
                            for warning in rec["warnings"]), rec["warnings"])
        for zone in rec["zones"]:
            self.assertEqual(1.0, zone["enrichment_factor"])
            self.assertIn("обогащать нельзя", zone["recommendation_ru"])

    # --- VANOS -----------------------------------------------------------

    def test_vanos_shift_only_in_stable_zones(self):
        good = fx.write_vanos_csv(self.dir / "vanos.csv", track_error_deg=0.8)
        rec = self.recommendation(good, "vanos")
        limit = float(SPEC["packages"]["vanos"]["criteria"]["max_shift_from_stock_deg"])
        self.assertTrue(rec["zones"])
        for zone in rec["zones"]:
            self.assertLessEqual(abs(zone["shift_deg"]), limit)

    def test_vanos_mechanics_problem_blocks_shift(self):
        bad = fx.write_vanos_csv(self.dir / "vanos_bad.csv", track_error_deg=4.5)
        rec = self.recommendation(bad, "vanos")
        self.assertTrue(rec["zones"])
        for zone in rec["zones"]:
            self.assertEqual(0.0, zone["shift_deg"])
            self.assertIn("механика", zone["recommendation_ru"])

    # --- интеграция с доказательством ------------------------------------

    def test_evidence_carries_recommendation_with_source_hash(self):
        csv_path = fx.write_test0_csv(self.dir / "test0_full.csv", full=True)
        outcome = analyzer.analyze_pack_log(csv_path, "ignition", 98)
        self.assertIn("pack_recommendation", outcome)
        rec = outcome["pack_recommendation"]
        self.assertEqual("pack_log", outcome["kind"])
        self.assertEqual(outcome["source_sha256"], rec["source_sha256"])
        self.assertEqual(analyzer.ANALYZER_VERSION, rec["analyzer_version"])
        self.assertEqual("ignition", rec["package"])

    def test_recommendation_is_deterministic(self):
        csv_path = fx.write_ignition_csv(self.dir / "twice.csv")
        first = self.recommendation(csv_path, "ignition", ron=98)
        second = self.recommendation(csv_path, "ignition", ron=98)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_tampered_recommendation_is_rejected_by_gate(self):
        csv_path = fx.write_test0_csv(self.dir / "tamper.csv", full=True)
        outcome = analyzer.analyze_pack_log(csv_path, "ignition", 98)
        evidence = dict(outcome)
        evidence["pack_recommendation"] = json.loads(
            json.dumps(outcome["pack_recommendation"]))
        evidence["pack_recommendation"]["zones"][0]["delta_deg"] = 9.9
        ok, problems = analyzer.verify_evidence(
            evidence, "ignition", search_dirs=(str(self.dir),))
        self.assertFalse(ok)
        self.assertTrue(any("EVIDENCE_TAMPERED" in problem for problem in problems),
                        problems)

    def test_untouched_recommendation_passes_gate(self):
        csv_path = fx.write_test0_csv(self.dir / "ok.csv", full=True)
        outcome = analyzer.analyze_pack_log(csv_path, "ignition", 98)
        ok, problems = analyzer.verify_evidence(
            outcome, "ignition", search_dirs=(str(self.dir),))
        self.assertTrue(ok, problems)

    def test_zone_delta_maps_map_cells(self):
        rec = {
            "zones": [
                {"index": {"rpm": 0, "load": 0}, "delta_deg": 1.5},
                {"index": {"rpm": 4, "load": 2}, "delta_deg": 0.0},
            ],
        }
        self.assertEqual(1.5, pack_recommendation.zone_delta(rec, 2500, 200))
        self.assertEqual(0.0, pack_recommendation.zone_delta(rec, 6000, 500))
        # Зона без данных — не выдумываем: берём fallback (по умолчанию 0).
        self.assertEqual(0.0, pack_recommendation.zone_delta(rec, 4500, 500))
        self.assertEqual(1.5, pack_recommendation.zone_delta(rec, 2500, 200,
                                                             fallback_deg=1.5))
        deltas = pack_recommendation.deltas_for_map(
            rec, [2500.0, 6000.0], [200.0, 500.0])
        self.assertEqual([1.5, 0.0, 0.0, 0.0], deltas)

    def test_unknown_pack_is_rejected(self):
        csv_path = fx.write_ignition_csv(self.dir / "x.csv")
        table = analyzer.read_log_table(csv_path)
        with self.assertRaises(ValueError):
            pack_recommendation.build_recommendation(table, "limits", SPEC)


class ZoneApiTests(unittest.TestCase):
    """API раскладки зон, которым пользуется генератор (PACKS_LOG_REQUIREMENTS §7.2)."""

    def setUp(self):
        self.evidence = {
            "kind": "pack_log",
            "analyzer_version": analyzer.ANALYZER_VERSION,
            "source_sha256": "a" * 64,
            "pack_recommendation": {
                "kind": "pack_recommendation",
                "package": "ignition",
                "analyzer_version": analyzer.ANALYZER_VERSION,
                "source_sha256": "a" * 64,
                "confidence": "high",
                "zones": [
                    {"index": {"rpm": 0, "load": 1}, "delta_deg": 1.5},
                    {"index": {"rpm": 1, "load": 1}, "delta_deg": 0.0},
                ],
            },
        }

    def trusted(self, **changes):
        evidence = json.loads(json.dumps(self.evidence))
        evidence.update(changes)
        return pack_recommendation.trusted_recommendation(
            evidence, "ignition", analyzer.ANALYZER_VERSION)

    def test_recommendation_of_the_same_log_is_trusted(self):
        recommendation, why = self.trusted()
        self.assertIsNotNone(recommendation, why)
        self.assertEqual("", why)

    def test_foreign_or_stale_recommendation_is_refused(self):
        self.assertEqual((None, "доказательство не приложено"),
                         pack_recommendation.trusted_recommendation(None, "ignition"))
        for change, needle in (
            ({"analyzer_version": "0.0.0"}, "версия анализатора"),
            ({"source_sha256": "b" * 64}, "не привязана к sha256"),
        ):
            recommendation, why = self.trusted(**change)
            self.assertIsNone(recommendation)
            self.assertIn(needle, why)
        recommendation, why = pack_recommendation.trusted_recommendation(
            self.evidence, "fuel", analyzer.ANALYZER_VERSION)
        self.assertIsNone(recommendation)
        self.assertIn("для пакета", why)

    def test_insufficient_confidence_is_not_used_for_writing(self):
        evidence = json.loads(json.dumps(self.evidence))
        evidence["pack_recommendation"]["confidence"] = "insufficient"
        recommendation, why = pack_recommendation.trusted_recommendation(
            evidence, "ignition", analyzer.ANALYZER_VERSION)
        self.assertIsNone(recommendation)
        self.assertIn("insufficient", why)

    def test_load_axis_limits_take_minimum_across_rpm_zones(self):
        recommendation = self.evidence["pack_recommendation"]
        # полоса 300..450: минимум из 1.5 и 0.0; полоса 150..300 — данных нет
        limits = pack_recommendation.deltas_for_load_axis(
            recommendation, [200.0, 400.0, 500.0], fallback_deg=1.5)
        self.assertEqual([1.5, 0.0, 1.5], limits)
        self.assertEqual(1, pack_recommendation.load_band_index(400.0))
        self.assertIsNone(pack_recommendation.load_band_index(700.0))

    def test_zone_field_and_point_lookup(self):
        recommendation = self.evidence["pack_recommendation"]
        # 2500 об/мин — зона rpm 0 (2000–3000), 400 мг/такт — полоса load 1
        zone = pack_recommendation.zone_for_point(recommendation, 2500.0, 400.0)
        self.assertEqual({"rpm": 0, "load": 1}, zone["index"])
        self.assertEqual(1.5, pack_recommendation.zone_field(
            recommendation, 2500.0, 400.0, "delta_deg"))
        # полоса load 0 (150–300 мг/такт) в зонах не измерена
        self.assertIsNone(pack_recommendation.zone_for_point(recommendation, 2500.0, 200.0))
        # обороты вне сетки зон
        self.assertIsNone(pack_recommendation.zone_for_point(recommendation, 7000.0, 400.0))
        self.assertIsNone(pack_recommendation.zone_field(
            recommendation, 2500.0, 400.0, "boost_bar"))

    def test_full_load_delta_takes_minimum_over_the_load_band(self):
        """Топливо: правка по оборотам = минимум запаса по полосам нагрузки."""
        recommendation = {
            "zones": [
                {"index": {"rpm": 0, "load": 1}, "lambda_min": 0.86},
                {"index": {"rpm": 0, "load": 2}, "lambda_min": 0.84},
                {"index": {"rpm": 1, "load": 1}, "lambda_min": 0.87},
            ],
        }
        limits = pack_recommendation.full_load_delta_by_rpm(
            recommendation, [2500.0, 3500.0, 4500.0], floor=0.82, requested=0.05)
        self.assertEqual([0.02, 0.05, 0.0], limits)  # 4500 — данных нет

    def test_full_load_delta_respects_lambda_floor(self):
        recommendation = {
            "zones": [{"index": {"rpm": 0, "load": 1}, "lambda_min": 0.80}],
        }
        limits = pack_recommendation.full_load_delta_by_rpm(
            recommendation, [2500.0], floor=0.82, requested=0.05)
        self.assertEqual([0.0], limits)  # зона уже ниже пола — не обогащаем

    def test_shift_limits_need_the_same_direction_as_the_log(self):
        recommendation = {
            "limits": {"max_track_error_deg": 3.0},
            "zones": [
                {"index": {"rpm": 0, "load": 1}, "shift_deg": 1.2,
                 "track_error_max_deg": 0.8},
                {"index": {"rpm": 0, "load": 2}, "shift_deg": -1.2,
                 "track_error_max_deg": 0.8},
            ],
        }
        limits = pack_recommendation.shift_limits_for_map(
            recommendation, [2500.0], [400.0, 500.0], 2.0)
        self.assertEqual([2.0, 0.0], limits)
        limits = pack_recommendation.shift_limits_for_map(
            recommendation, [2500.0], [400.0, 500.0], -2.0)
        self.assertEqual([0.0, 2.0], limits)

    def test_zones_carry_index_and_are_sorted(self):
        csv_path = fx.write_ignition_csv(Path(tempfile.mkdtemp()) / "z.csv")
        outcome = analyzer.analyze_pack_log(csv_path, "ignition", 98)
        for zone in outcome["pack_recommendation"]["zones"]:
            self.assertIn("index", zone)
            self.assertIn("rpm", zone["index"])
            self.assertIn("load", zone["index"])
            self.assertEqual(
                zone["delta_deg"],
                pack_recommendation.zone_delta(
                    outcome["pack_recommendation"],
                    pack_recommendation.RPM_EDGES[zone["index"]["rpm"]] + 1,
                    pack_recommendation.LOAD_EDGES[zone["index"]["load"]] + 1))


class RecommendationCliTests(unittest.TestCase):
    def test_recommend_cli_prints_only_recommendation(self):
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            csv_path = fx.write_ignition_csv(Path(directory) / "cli.csv")
            result = subprocess.run(
                [sys.executable, "tools/pack_log_analyzer.py", "--pack",
                 "ignition", "--csv", str(csv_path), "--ron", "98",
                 "--recommend"],
                cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("pack_recommendation", payload["kind"])
        self.assertTrue(payload["zones"])
        self.assertNotIn("results", payload)


if __name__ == "__main__":
    unittest.main()

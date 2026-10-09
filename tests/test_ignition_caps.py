# -*- coding: utf-8 -*-
"""Единый источник потолков добавки зажигания (задание раунда 4, §2.1).

Проверяет, что числа живут в одном месте (``docs/pack_log_requirements.json``)
и совпадают с таблицей §1 задания ``docs/TASK_STAGE1_RU.md``:

* без лога (Stage 1) — до +3,0° на карте RON98 для профиля RON98, до +1,5°
  для RON95, RON91 — 0, профиль не выбран — консервативные +1,5°;
* с логом детонации (Stage 2) — до +5,0° для RON98 и RON95, RON91 — 0;
* генераторы MS43/MS45 не держат своих чисел: их константы выводятся из той же
  спецификации;
* алгоритм раскладки (``tools/ignition_layout.py``) детерминирован: потолок не
  нарушается, соседи отличаются не больше шага, прижатие — к сетке вниз.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import ignition_caps as caps  # noqa: E402
from tools import ignition_layout as layout  # noqa: E402

SPEC = json.loads((ROOT / "docs" / "pack_log_requirements.json").read_text(encoding="utf-8"))


class SpecTableTests(unittest.TestCase):
    def test_json_matches_owner_table(self) -> None:
        caps.check_matches_task()  # не должно бросить
        gates = SPEC["packages"]["ignition"]["gates"]
        expected = caps.expected_table()
        for key in ("delta_without_log_deg", "delta_with_log_deg"):
            self.assertEqual(float(expected[key]), float(gates[key]), key)
        for key in ("octane_no_log_deg", "octane_with_log_deg"):
            self.assertEqual(
                {k: float(v) for k, v in expected[key].items()},
                {k: float(v) for k, v in gates[key].items()},
                key,
            )

    def test_cap_values(self) -> None:
        self.assertEqual(3.0, caps.cap_no_log_deg("ron98"))
        self.assertEqual(1.5, caps.cap_no_log_deg("ron95"))
        self.assertEqual(0.0, caps.cap_no_log_deg("ron91"))
        self.assertEqual(1.5, caps.cap_no_log_deg(None))
        self.assertEqual(5.0, caps.cap_with_log_deg("ron98"))
        self.assertEqual(5.0, caps.cap_with_log_deg("ron95"))
        self.assertEqual(0.0, caps.cap_with_log_deg("ron91"))
        self.assertEqual(5.0, caps.cap_with_log_deg(None))
        self.assertEqual(3.0, caps.cap_for("ron98", has_verified_log=False))
        self.assertEqual(5.0, caps.cap_for("ron98", has_verified_log=True))
        self.assertTrue(caps.is_profile_rejected("ron91"))
        self.assertFalse(caps.is_profile_rejected("ron95"))

    def test_drift_detection(self) -> None:
        import tempfile
        from tools import ignition_caps as module

        with tempfile.TemporaryDirectory() as directory:
            broken = json.loads(json.dumps(SPEC))
            broken["packages"]["ignition"]["gates"]["delta_without_log_deg"] = 2.5
            path = Path(directory) / "broken.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            original = module.REQUIREMENTS_PATH
            module.REQUIREMENTS_PATH = path
            module.reset_cache_for_tests()
            try:
                with self.assertRaises(caps.CapSpecError):
                    module.check_matches_task()
                # и потолок без лога тоже поехал — модуль честно читает json,
                # а не свои «дефолты»
                self.assertEqual(2.5, module.cap_no_log_deg("ron98"))
            finally:
                module.REQUIREMENTS_PATH = original
                module.reset_cache_for_tests()


class NoHardcodedCapsTests(unittest.TestCase):
    """Потолки не должны дублироваться в коде: проверяем места из прошлого."""

    def test_ms43_variants_use_shared_caps(self) -> None:
        from MS43.tools import stages as ms43_stages

        for variant, spec in ms43_stages.VARIANTS.items():
            self.assertEqual(
                caps.delta_without_log_deg(),
                spec["ignition_without_log_deg"],
                f"MS43 {variant}: ignition_without_log_deg разошёлся со спецификацией",
            )
            self.assertEqual(
                caps.delta_with_log_deg(),
                spec["ignition_with_log_deg"],
                f"MS43 {variant}: ignition_with_log_deg разошёлся со спецификацией",
            )

    def test_ms45_variants_and_aliases_use_shared_caps(self) -> None:
        from MS45.tools import stages as ms45_stages

        self.assertEqual(caps.delta_with_log_deg(), ms45_stages.IGNITION_DELTA_MAX_DEG)
        self.assertEqual(caps.delta_without_log_deg(), ms45_stages.IGNITION_DELTA_NO_LOG_DEG)
        for variant, spec in ms45_stages.VARIANTS.items():
            self.assertEqual(
                caps.delta_without_log_deg(), spec["ignition_without_log_deg"],
                f"MS45 {variant}",
            )
            self.assertEqual(
                caps.delta_with_log_deg(), spec["ignition_with_log_deg"],
                f"MS45 {variant}",
            )

    def test_web_thresholds_match_spec(self) -> None:
        """Сайт держит потолки в web/request.js — сверяем цифры с json."""
        text = (ROOT / "web" / "request.js").read_text(encoding="utf-8")
        self.assertIn(
            "ignitionMaxDeg: %s" % _js_num(caps.delta_without_log_deg()), text,
            "сайт: потолок без лога разошёлся со спецификацией",
        )
        self.assertIn(
            "ignitionLogCapDeg: %s" % _js_num(caps.delta_with_log_deg()), text,
            "сайт: потолок с логом разошёлся со спецификацией",
        )
        for key in ("ron98", "ron95", "ron91"):
            self.assertIn(
                "%s: %s," % (key, _js_num(caps.cap_no_log_deg(key))), text,
                f"сайт: потолок профиля {key} разошёлся со спецификацией",
            )


def _js_num(value: float) -> str:
    text = repr(float(value))
    if text.endswith(".0"):
        text = text[:-2]
    return text


class LayoutMathTests(unittest.TestCase):
    def test_shape_bounds(self) -> None:
        self.assertEqual(0.0, layout.shape_factor(200.0))
        self.assertEqual(0.0, layout.shape_factor(50.0))
        self.assertEqual(0.5, layout.shape_factor(400.0))
        self.assertEqual(1.0, layout.shape_factor(600.0))
        self.assertEqual(1.0, layout.shape_factor(5000.0))

    def test_plan_never_exceeds_margin_or_ceiling(self) -> None:
        rows, cols = 5, 4
        stock = [10.0, 20.0, 30.0, 40.0] * rows
        mbt = [10.6, 20.0, 34.0, 28.0] * rows  # запас: 0.6 / 0 / 4 / (минус)
        load = [100.0, 250.0, 600.0, 1400.0]
        adds, note = layout.plan_additions(stock, mbt, 3.0, load, cols, rows)
        self.assertEqual("", note)
        for i, (s, m, a) in enumerate(zip(stock, mbt, adds)):
            self.assertLessEqual(a, max(0.0, m - s) + 1e-9)
            self.assertLessEqual(a, 3.0 + 1e-9)
            self.assertAlmostEqual(a % 0.375, 0.0, places=9)
        # ячейка у MBT (запас 0) и с отрицательным запасом — без добавки
        self.assertEqual(0.0, adds[1])
        self.assertEqual(0.0, adds[3])
        # 0.6*0.5=0.3 → прижимается к 0 (ниже кванта), на 250 мг форма 0.125
        self.assertEqual(0.0, adds[0])
        self.assertEqual(0.0, adds[7])  # 4°×0.5=2 ×0.125=0.25 → 0

    def test_smooth_only_reduces(self) -> None:
        rows, cols = 1, 5
        adds = layout.smooth_down([3.0, 3.0, 0.0, 3.0, 3.0], rows, cols, max_step=1.5)
        self.assertLessEqual(max(adds), 3.0)
        for i in range(1, len(adds)):
            self.assertLessEqual(abs(adds[i] - adds[i - 1]), 1.5 + 1e-9)
        # нулевой запретный столбец не подтянулся вверх
        self.assertEqual(0.0, adds[2])
        # соседи нулевой ячейки стали не больше шага
        self.assertLessEqual(adds[1], 1.5 + 1e-9)
        self.assertLessEqual(adds[3], 1.5 + 1e-9)

    def test_2d_jump_limit_exact(self) -> None:
        rows, cols = 4, 4
        grid = [3.0] * (rows * cols)
        grid[0] = 0.0
        out = layout.smooth_down(grid, rows, cols, max_step=1.5)
        self.assertLessEqual(layout.max_neighbour_jump(out, rows, cols), 1.5 + 1e-9)
        self.assertEqual(0.0, out[0])
        self.assertEqual(1.5, out[cols])  # манхэттенское расстояние 1 → шаг 1
        self.assertEqual(1.5, out[1])

    def test_quantize_down_never_up(self) -> None:
        self.assertEqual([0.0, 0.375, 0.375], layout.quantize_down([0.1, 0.375, 0.7]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

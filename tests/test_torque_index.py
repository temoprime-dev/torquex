# -*- coding: utf-8 -*-
"""tools/torque_index.py — офлайн-индекс ожидаемого момента (раунд 4 §2а.2).

Математика интерполяции проверяется на руки посчитанных числах, остальное —
на реальном стоке 430069 из репозитория: кривая читается через штатный
загрузчик XDF (не регулярками), сток против стока даёт нулевой индекс,
плоская добавка не перелетает MBT.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import torque_index  # noqa: E402

STOCK = ROOT / "MS43" / "firmware" / "Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin"


class CurveMathTests(unittest.TestCase):
    CURVE_X = [0.0, -6.0, -9.0, -12.0, -18.0, -24.0, -33.0, -42.0]
    CURVE_Y = [1.0, 0.992, 0.984, 0.953, 0.844, 0.703, 0.438, 0.125]

    def test_interpolation_edges_and_mid(self) -> None:
        self.assertEqual(1.0, torque_index.interpolate_curve(self.CURVE_X, self.CURVE_Y, 3.0))
        self.assertEqual(0.125, torque_index.interpolate_curve(self.CURVE_X, self.CURVE_Y, -50.0))
        self.assertAlmostEqual(0.996, torque_index.interpolate_curve(
            self.CURVE_X, self.CURVE_Y, -3.0), places=9)
        # середина участка -6..-9: 0.992 - (0.992-0.984)/2
        self.assertAlmostEqual(0.988, torque_index.interpolate_curve(
            self.CURVE_X, self.CURVE_Y, -7.5), places=9)

    def test_delta_index_zero_when_nothing_changed(self) -> None:
        stock = [10.0, 12.0, 20.0]
        mbt = [14.0, 12.0, 18.0]  # третья ячейка уже выше оптимума
        out = torque_index.deltas_index(stock, stock, mbt, self.CURVE_X, self.CURVE_Y)
        self.assertEqual([0.0, 0.0, 0.0], [round(v, 12) for v in out])

    def test_delta_index_follows_margin_only(self) -> None:
        # +2° там, где запас есть, не трогая ячейку у MBT и «переbrink» ячейку
        stock = [10.0, 12.0, 20.0]
        patched = [12.0, 12.0, 20.0]
        mbt = [14.0, 12.0, 18.0]
        out = torque_index.deltas_index(stock, patched, mbt, self.CURVE_X, self.CURVE_Y)
        # сток был в −4° от оптимума, стал в −2°: прирост = eff(-2) - eff(-4)
        self.assertAlmostEqual(
            torque_index.interpolate_curve(self.CURVE_X, self.CURVE_Y, -2.0)
            - torque_index.interpolate_curve(self.CURVE_X, self.CURVE_Y, -4.0),
            out[0], places=9)
        self.assertGreater(out[0], 0.0)
        self.assertEqual(0.0, out[1])
        self.assertEqual(0.0, out[2])


@unittest.skipUnless(STOCK.exists(), "сток 430069 не в репозитории")
class RealStockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.image = STOCK.read_bytes()

    def test_stock_vs_stock_is_zero(self) -> None:
        result = torque_index.compute_ms43(self.image, self.image, flat_deg=None)
        self.assertEqual("MS43", result["platform"])
        self.assertEqual("430069", result["version"])
        patched = result["patched"]
        self.assertEqual(0.0, patched["mean_pct"])
        self.assertEqual(0, patched["changed_cells"])

    def test_curve_matches_xdf(self) -> None:
        result = torque_index.compute_ms43(self.image, None, flat_deg=None)
        curve = result["curve"]
        self.assertEqual(8, len(curve["x_deg"]))
        self.assertAlmostEqual(1.0, curve["eff"][0], places=3)
        self.assertLess(curve["eff"][-1], 0.5)  # -42°: момент почти потерян
        self.assertTrue(all(x <= 0.0 for x in curve["x_deg"]))

    def test_curve_read_from_own_version_definition(self) -> None:
        """430037 — своя кривая из своего определения, не мусор с чужих адресов."""
        stock = (ROOT / "MS43/firmware_archive/E39_530__b137j54g_Ca430037_Stok.bin")
        if not stock.is_file():
            self.skipTest("нет стока 430037 в архиве")
        result = torque_index.compute_ms43(stock.read_bytes(), None, flat_deg=None)
        curve = result["curve"]
        self.assertIsNotNone(curve, result.get("curve_error"))
        self.assertAlmostEqual(1.0, curve["eff"][0], places=3)
        self.assertTrue(all(x <= 0.0 for x in curve["x_deg"]))

    def test_version_without_definition_is_honest(self) -> None:
        """Без определения версия не получает выдуманный индекс: либо кривая,
        либо curve_error и никаких процентов."""
        with self.assertRaises(LookupError):
            torque_index._ms43_curve(b"\x00" * 64, "439999")
    def test_markdown_is_cp1251_safe_on_console(self) -> None:
        """Windows-консоль (cp1251) не падает на выводе индекса."""
        result = torque_index.compute_ms43(self.image, None, flat_deg=3.0)
        torque_index.to_markdown(result).encode("cp1251")

    def test_flat_reference_never_exceeds_mbt(self) -> None:
        result = torque_index.compute_ms43(self.image, None, flat_deg=3.0)
        flat = result["flat_reference"]
        self.assertGreater(flat["mean_pct"], 0.0)
        self.assertGreater(flat["changed_cells"], 0)
        self.assertLessEqual(flat["max_pct"], 15.0)

    def test_markdown_mentions_limits(self) -> None:
        result = torque_index.compute_ms43(self.image, None, flat_deg=3.0)
        md = torque_index.to_markdown(result)
        for fragment in ("не дино", "мощность не обещается", "ip_eff_iga_mon"):
            self.assertIn(fragment, md)


class Ms45HonestyTests(unittest.TestCase):
    STOCK45 = (ROOT / "MS45" / "firmware" / "MS45.0_456BO00S_E83"
               / "7561448_0044560BO00S0CO0R.bin")

    @unittest.skipUnless(STOCK45.exists(), "сток BO00S не в репозитории")
    def test_ms45_prints_curve_and_refuses_per_cell(self) -> None:
        result = torque_index.compute_ms45(self.STOCK45.read_bytes())
        self.assertEqual("MS45", result["platform"])
        self.assertIsNone(result["per_cell"])
        self.assertIn("ip_iga_ref__n__maf", result["why_no_per_cell"])
        self.assertEqual(8, len(result["curve"]["eff"]))
        self.assertAlmostEqual(1.0, result["curve"]["eff"][0], places=2)
        md = torque_index.to_markdown(result)
        self.assertIn("не считается", md)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

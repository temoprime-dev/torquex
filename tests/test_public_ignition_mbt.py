# -*- coding: utf-8 -*-
"""Публичный Stage 1 (без linked MBT) не должен уводить угол выше опоры optm_tco_2.

Регрессия раунда 5b: публичный путь вызывал plan_additions без MBT-потолка, и на
реальной 430069 число ячеек «угол > optm» росло с 33 до 202 (до +4,5° над опорой),
хотя сама опора не поднималась.
"""

import unittest
from pathlib import Path

from MS43.tools import policy as ms43_policy
from MS43.tools import stages as ms43_stages

ROOT = Path(__file__).resolve().parents[1]
STOCK = ROOT / "MS43" / "firmware" / "Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin"


@unittest.skipUnless(STOCK.exists(), "нет образа 430069")
class PublicIgnitionStaysUnderMbtTests(unittest.TestCase):
    def test_public_path_does_not_add_cells_above_optm(self) -> None:
        image = STOCK.read_bytes()
        policy = ms43_policy.load_policy()
        entry = policy.objects["ip_iga_ron_98_pl_ivvt__n__maf"]
        caps = ms43_stages._optm_caps(image, policy)
        self.assertIsNotNone(caps)
        before = ms43_stages._read_all(image, entry)
        request = {
            "stage": "stage1", "variant": "v1", "octane": "ron98",
            "acknowledge_legality": True, "_allow_linked_ignition": False,
            "intents": [{"id": "ign1", "kind": "ignition", "delta_deg": 3.0}],
        }
        result = ms43_stages.build("stage1", image, request, policy=policy)
        self.assertEqual("BUILT", result.status, result.findings)
        after = ms43_stages._read_all(result.result.image, entry)
        over_before = sum(1 for s, c in zip(before, caps) if s > c + 1e-9)
        over_after = sum(1 for s, c in zip(after, caps) if s > c + 1e-9)
        self.assertEqual(over_before, over_after)
        self.assertTrue(any(a > b for a, b in zip(after, before)))


if __name__ == "__main__":
    unittest.main()

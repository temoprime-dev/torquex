"""Тесты ядра патчинга MS45 (``MS45/tools/calibration.py``).

Главное, что здесь проверяется:

* отказы — это отказы: каждый запрещённый случай даёт свой код, а не
  предупреждение;
* после применения плана измениться обязаны **только** ячейки плана и байты
  сумм, и ничего больше (в том числе в полном образе 1 МиБ — побайтовое
  равенство вне области калибровки);
* итоговый файл проходит проверку обеих сумм.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from MS45.tools import calibration as cal
from MS45.tools import checksums as ck
from MS45.tools import policy as pl
from MS45.tests.support import unknown_family_image  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
MS45 = ROOT / "MS45"
SPDATEN = MS45 / "firmware_spdaten" / "cal"
DUMPS = MS45 / "firmware_dumps"

BO00_CAL = MS45 / "firmware" / "MS45.0_456BO00S_E83" / "7561448_0044560BO00S0CO0R.bin"
BO00_EU = SPDATEN / "MS45.0_0044560BO00S0KJ0F_P7576179_cal_116K.bin"
LO00_CAL = SPDATEN / "MS45.1_0044570LO00S01J2R_P7561517_cal_116K.bin"
BN00_CAL = SPDATEN / "MS45.0_0044560BN00S07F0G_P7550088_cal_116K.bin"
#: Семейство без определений: на нём проверяются отказы по версии.
BL00_CAL = SPDATEN / "MS45.0_0044560BL00S0IA3N_P7537853_cal_116K.bin"
FULL_DUMP = DUMPS / "MS45.0_0044560BN_fullread_ORI.bin"
MPC_DUMP = DUMPS / "MS45.0_0044560BN_MPC_ORI.bin"
POLICY = pl.load_policy()


def _codes(plan: cal.PatchPlan) -> list[str]:
    return [finding.code for finding in plan.findings]


def _request(symbol: str, *values: float) -> dict:
    return {"symbol": symbol, "values": list(values)}


def _plan(image: bytes, requests: list[dict], acknowledgements: set[str] | None = None) -> cal.PatchPlan:
    return cal.plan_changes(image, requests, policy=POLICY, acknowledgements=acknowledgements)


def _synthetic_full_image(cal_image: Path | bytes) -> bytes:
    """Полный образ 1 МиБ: калибровка на своём месте, остальное — из реального дампа."""
    data = cal_image.read_bytes() if isinstance(cal_image, Path) else cal_image
    dump = bytearray(FULL_DUMP.read_bytes())
    dump[ck.EXT_CAL_OFFSET : ck.EXT_CAL_OFFSET + ck.CAL_SIZE] = data
    return bytes(dump)


class TestPlanRefusals(unittest.TestCase):
    """Каждый запрет — отдельный код отказа."""

    def setUp(self) -> None:
        self.image = BO00_CAL.read_bytes()

    def test_happy_path_planned(self) -> None:
        plan = _plan(self.image, [_request("c_vs_max", 255)])
        self.assertEqual(plan.status, "PLANNED", _codes(plan))
        self.assertEqual(len(plan.planned_cells), 1)
        self.assertEqual(plan.family, "4560BO00S")
        self.assertEqual(plan.kind, "cal")

    def test_read_only(self) -> None:
        for symbol in ("c_n_lim_min", "c_t_min_pu_msr", "val_mo3_swi_cal_cks"):
            plan = _plan(self.image, [_request(symbol, 100)])
            self.assertIn(pl.CODE_READ_ONLY, _codes(plan), symbol)

    def test_symbol_not_allowed(self) -> None:
        # ip_iga_bas_ron_98__n__maf и флаги слоя «без катализатора» с задания 3
        # в allowlist — проверяем то, что туда не входит: карта момента,
        # битовая маска OBD, таблицы ПХХ.
        for symbol in ("ip_fac_tq_req_driv_mon", "c_obd_state_2", "ip_n_min_puc__tco__gr_mt"):
            plan = _plan(self.image, [_request(symbol, 10)])
            self.assertIn(pl.CODE_SYMBOL_NOT_ALLOWED, _codes(plan), symbol)

    def test_policy_range(self) -> None:
        plan = _plan(self.image, [_request("c_vs_max", 90)])
        self.assertIn(pl.CODE_POLICY_RANGE_VIOLATION, _codes(plan))
        plan = _plan(self.image, [_request("id_n_max_mt", 7400)])
        self.assertIn(pl.CODE_POLICY_RANGE_VIOLATION, _codes(plan))

    def test_value_not_representable(self) -> None:
        plan = _plan(self.image, [_request("c_vs_max", 300)])
        self.assertIn(pl.CODE_VALUE_NOT_REPRESENTABLE, _codes(plan))

    def test_quantization_within_tolerance_is_allowed(self) -> None:
        """Для «целых» констант с шагом 1 ошибка округления мала и не мешает."""
        plan = _plan(self.image, [_request("c_vs_max", 254.9)])
        self.assertEqual(plan.status, "PLANNED", _codes(plan))
        self.assertAlmostEqual(plan.cells[0].quantization_error_percent, 0.0392, places=3)

    def test_quantization_too_coarse_is_refused(self) -> None:
        """Грубый масштаб (шаг 10) и запрос между кодами — отказ."""
        import dataclasses

        entry = dataclasses.replace(
            POLICY.for_family("4560BO00S").entry("c_vs_max"),
            scale_a=10.0,
            minimum=0.0,
            maximum=2550.0,
            max_delta_percent=None,
        )
        coarse = dataclasses.replace(
            POLICY.for_family("4560BO00S"),
            objects={**POLICY.for_family("4560BO00S").objects, "c_vs_max": entry},
        )
        policy = dataclasses.replace(POLICY, families={**POLICY.families, "4560BO00S": coarse})
        plan = cal.plan_changes(self.image, [_request("c_vs_max", 405)], policy=policy)
        self.assertIn(pl.CODE_QUANTIZATION_TOO_COARSE, _codes(plan))

    def test_delta_too_large(self) -> None:
        lo = LO00_CAL.read_bytes()
        # сток 6800..6900 rpm, предел отклонения 10% => 6000 слишком далеко
        plan = _plan(lo, [_request("id_n_max_mt", 6000)])
        self.assertIn(pl.CODE_DELTA_TOO_LARGE, _codes(plan))

    def test_acknowledgement_required_for_pops(self) -> None:
        plan = _plan(self.image, [_request("c_t_min_pu", 0.5)])
        self.assertEqual(_codes(plan), [cal.CODE_ACKNOWLEDGEMENT_REQUIRED])
        plan = _plan(self.image, [_request("c_t_min_pu", 0.5)], {"no-catalyst"})
        self.assertEqual(plan.status, "PLANNED", _codes(plan))

    def test_cell_count(self) -> None:
        plan = _plan(self.image, [_request("id_n_max_mt", 6500, 6500, 6500)])
        self.assertIn(cal.CODE_CELL_COUNT, _codes(plan))

    def test_duplicate_symbol(self) -> None:
        plan = _plan(self.image, [_request("c_vs_max", 255), _request("c_vs_max", 250)])
        self.assertIn(cal.CODE_DUPLICATE_SYMBOL, _codes(plan))

    def test_nothing_to_do(self) -> None:
        current = BO00_CAL.read_bytes()[0x18DC]
        plan = _plan(self.image, [_request("c_vs_max", float(current))])
        self.assertIn(cal.CODE_NOTHING_TO_DO, _codes(plan))

    def test_unsupported_family(self) -> None:
        """Все десять целевых семейств поддержаны (шаг 3), поэтому отказ по версии
        проверяется на синтезированном идентификаторе чужого семейства."""
        plan = _plan(unknown_family_image(BL00_CAL.read_bytes()), [_request("c_vs_max", 255)])
        self.assertEqual(_codes(plan), [pl.CODE_CAL_VERSION_UNSUPPORTED])

    def test_mpc_image(self) -> None:
        plan = _plan(MPC_DUMP.read_bytes(), [_request("c_vs_max", 255)])
        self.assertEqual(_codes(plan), [ck.MpcImageError.code])

    def test_wrong_size(self) -> None:
        plan = _plan(bytes(0x10000), [_request("c_vs_max", 255)])
        self.assertEqual(_codes(plan), [ck.ImageFormatError.code])

    def test_broken_source_checksum(self) -> None:
        broken = bytearray(self.image)
        broken[0x100] ^= 0xFF
        plan = _plan(bytes(broken), [_request("c_vs_max", 255)])
        self.assertEqual(_codes(plan), [ck.SourceChecksumError.code])

    def test_plan_records_checksum_cells(self) -> None:
        plan = _plan(self.image, [_request("c_vs_max", 255)])
        self.assertEqual(plan.checksum_cells[:4], [0x100, 0x101, 0x102, 0x103])
        self.assertIn(0x5820, plan.checksum_cells)


class TestApply(unittest.TestCase):
    """Применение плана: только плановые ячейки, суммы, итоговая проверка."""

    def setUp(self) -> None:
        self.image = BO00_CAL.read_bytes()

    def test_single_cell_and_checksums(self) -> None:
        plan = _plan(self.image, [_request("c_vs_max", 255)])
        result = cal.apply_plan(self.image, plan)
        changed = {i for i, (a, b) in enumerate(zip(self.image, result.image)) if a != b}
        allowed = set(plan.checksum_cells) | {0x18DC}
        self.assertLessEqual(changed, allowed)
        self.assertIn(0x18DC, changed)
        self.assertLessEqual({0x100, 0x101, 0x102, 0x103}, changed)
        self.assertTrue(ck.verify_bytes(result.image).ok)
        self.assertEqual(result.changed_cells, 1)
        self.assertTrue(any(name == "CRC-32" for name, *_ in result.checksum_words))
        self.assertEqual(result.sha256, hashlib.sha256(result.image).hexdigest())

    def test_full_package_changes_only_plan_and_checksums(self) -> None:
        requests = [
            _request("c_vs_max", 255),
            _request("id_n_max_mt", 6800),
            _request("id_n_max_h_mt", 6800),
            _request("c_t_min_pu", 0.5),
            _request("c_t_min_pu_vs", 0.5),
            _request("c_t_min_pu_rng_l", 0.5),
        ]
        plan = _plan(self.image, requests, {"no-catalyst"})
        self.assertEqual(plan.status, "PLANNED", _codes(plan))
        result = cal.apply_plan(self.image, plan)
        planned = {offset for cell in plan.planned_cells for offset in range(cell.image_offset, cell.image_offset + cell.size_bytes)}
        changed = {i for i, (a, b) in enumerate(zip(self.image, result.image)) if a != b}
        self.assertTrue(changed)
        self.assertLessEqual(changed, planned | set(plan.checksum_cells))
        self.assertTrue(ck.verify_bytes(result.image).ok)
        # MO3 не должна меняться: правки вне области 0x240..0x7C8.
        self.assertFalse(any(name.startswith("MO3") for name, *_ in result.checksum_words))

    def test_patch_inside_mo3_updates_mo3(self) -> None:
        """Правка внутри области MO3 меняет и пару MO3, и CRC-32."""
        image = bytearray(LO00_CAL.read_bytes())
        store = ck.MO3_STORE_OFFSET["4570LO00S"]
        # Сдвигаем одну константу в область MO3 нельзя (она не в allowlist), поэтому
        # правим план вручную: имитируем правку ячейки внутри области MO3.
        plan = _plan(bytes(image), [_request("c_vs_max", 250)])
        plan.planned_cells[0].image_offset = 0x400  # внутри области MO3
        plan.planned_cells[0].cal_offset = 0x400
        image = bytearray(LO00_CAL.read_bytes())
        image[0x400] ^= 0x01
        rebuilt, _ = ck.recompute(bytes(image))
        source = bytearray(LO00_CAL.read_bytes())
        # ячейка плана: байт 0x400 (сырое значение 16-битной пары)
        plan.planned_cells[0].raw_before = int.from_bytes(bytes(source[0x400:0x402]), "big")
        plan.planned_cells[0].raw_after = int.from_bytes(bytes(rebuilt[0x400:0x402]), "big")
        plan.planned_cells[0].size_bytes = 2
        result = cal.apply_plan(bytes(source), plan)
        self.assertTrue(ck.verify_bytes(result.image).ok)
        self.assertTrue(any(name == "CRC-32" for name, *_ in result.checksum_words))
        self.assertEqual(result.image[store : store + 8], rebuilt[store : store + 8])

    def test_rejected_plan_cannot_be_applied(self) -> None:
        plan = _plan(self.image, [_request("c_n_lim_min", 7400)])
        with self.assertRaises(cal.PatchError) as ctx:
            cal.apply_plan(self.image, plan)
        self.assertEqual(ctx.exception.code, pl.CODE_READ_ONLY)

    def test_wrong_source_image(self) -> None:
        plan = _plan(self.image, [_request("c_vs_max", 255)])
        other = LO00_CAL.read_bytes()
        with self.assertRaises(cal.PatchError) as ctx:
            cal.apply_plan(other, plan)
        self.assertEqual(ctx.exception.code, ck.SourceChecksumError.code)

    def test_full_image_untouched_outside_calibration(self) -> None:
        """В полном образе 1 МиБ вне области калибровки — байт в байт как было."""
        image = _synthetic_full_image(BO00_CAL)
        plan = _plan(image, [_request("c_vs_max", 255)])
        self.assertEqual(plan.kind, "full", _codes(plan))
        result = cal.apply_plan(image, plan)
        cal_start = ck.EXT_CAL_OFFSET
        cal_end = ck.EXT_CAL_OFFSET + ck.CAL_SIZE
        outside = [
            index
            for index in range(len(image))
            if not cal_start <= index < cal_end and image[index] != result.image[index]
        ]
        self.assertEqual(outside, [])
        self.assertTrue(ck.verify_bytes(result.image).ok)
        self.assertEqual(result.image[:cal_start], image[:cal_start])
        self.assertEqual(result.image[cal_end:], image[cal_end:])

    def test_full_image_supported_family_and_checksum_offsets(self) -> None:
        image = _synthetic_full_image(BO00_CAL)
        plan = _plan(image, [_request("c_vs_max", 255)])
        result = cal.apply_plan(image, plan)
        self.assertEqual(result.image[0x18DC + ck.EXT_CAL_OFFSET], 255)
        self.assertEqual(result.image[0x18DC], image[0x18DC])  # в файле дампа это другое место
        changed = {i for i, (a, b) in enumerate(zip(image, result.image)) if a != b}
        self.assertTrue(all(ck.EXT_CAL_OFFSET <= i < ck.EXT_CAL_OFFSET + ck.CAL_SIZE for i in changed))

    def test_source_must_be_stock(self) -> None:
        """Из уже тюнингованного файла новую правку не строим: это не сток."""
        plan = _plan(self.image, [_request("id_n_max_mt", 7000)])
        result = cal.apply_plan(self.image, plan)
        again = _plan(result.image, [_request("id_n_max_mt", 6800)])
        self.assertIn(cal.CODE_SOURCE_NOT_STOCK, _codes(again))
        self.assertEqual(again.status, "REJECTED")

    def test_second_apply_from_patched_image_is_nothing_to_do(self) -> None:
        plan = _plan(self.image, [_request("c_vs_max", 255)])
        result = cal.apply_plan(self.image, plan)
        again = _plan(result.image, [_request("c_vs_max", 255)])
        self.assertIn(cal.CODE_NOTHING_TO_DO, _codes(again))


class TestTableWrites(unittest.TestCase):
    """Запись таблиц: все ячейки разом и одна ячейка отдельно."""

    def test_broadcast_value_to_all_cells(self) -> None:
        image = BO00_CAL.read_bytes()
        plan = _plan(image, [_request("id_n_max_mt", 6800)])
        self.assertEqual(len(plan.cells), 9)
        self.assertEqual(len(plan.planned_cells), 9)
        result = cal.apply_plan(image, plan)
        entry = POLICY.for_family("4560BO00S").entry("id_n_max_mt")
        raw = int.from_bytes(result.image[entry.address : entry.address + 2], "big")
        for index in range(entry.element_count):
            offset = entry.address + index * 2
            self.assertEqual(int.from_bytes(result.image[offset : offset + 2], "big"), 6800)
        self.assertEqual(raw, 6800)

    def test_per_cell_values(self) -> None:
        image = BO00_CAL.read_bytes()
        plan = _plan(image, [_request("id_n_max_mt", 6500, 6600, 6700, 6750, 6800, 6800, 6800, 6800, 6800)])
        self.assertEqual(len(plan.cells), 9)
        result = cal.apply_plan(image, plan)
        entry = POLICY.for_family("4560BO00S").entry("id_n_max_mt")
        values = [
            int.from_bytes(result.image[entry.address + i * 2 : entry.address + i * 2 + 2], "big")
            for i in range(9)
        ]
        self.assertEqual(values, [6500, 6600, 6700, 6750, 6800, 6800, 6800, 6800, 6800])

    def test_none_leaves_cell_untouched(self) -> None:
        image = BO00_CAL.read_bytes()
        plan = _plan(image, [_request("id_n_max_mt", None, None, None, None, None, None, None, None, 6900)])
        self.assertEqual(len(plan.cells), 1)
        self.assertEqual(plan.cells[0].index, 8)

    def test_pops_package(self) -> None:
        image = BO00_CAL.read_bytes()
        plan = _plan(
            image,
            [_request("c_t_min_pu", 0.5), _request("c_t_min_pu_vs", 0.5), _request("c_t_min_pu_rng_l", 0.5)],
            {"no-catalyst"},
        )
        self.assertEqual(plan.status, "PLANNED", _codes(plan))
        result = cal.apply_plan(image, plan)
        for symbol in ("c_t_min_pu", "c_t_min_pu_vs", "c_t_min_pu_rng_l"):
            entry = POLICY.for_family("4560BO00S").entry(symbol)
            raw = int.from_bytes(result.image[entry.address : entry.address + 2], "big")
            self.assertAlmostEqual(entry.to_engineering(raw), 0.5, places=9, msg=symbol)


class TestCli(unittest.TestCase):
    """CLI ядра: план, запись, JSON, dry-run."""

    def test_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.bin"
            code = cal.main(
                ["--image", str(BO00_CAL), "--set", "c_vs_max=255", "--dry-run", "--out", str(out)]
            )
            self.assertEqual(code, 0)
            self.assertFalse(out.exists())

    def test_write_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.bin"
            report = Path(tmp) / "plan.json"
            code = cal.main(
                [
                    "--image",
                    str(BO00_CAL),
                    "--set",
                    "c_vs_max=255",
                    "--set",
                    "id_n_max_mt=6800",
                    "--out",
                    str(out),
                    "--json-out",
                    str(report),
                ]
            )
            self.assertEqual(code, 0)
            self.assertTrue(out.exists())
            self.assertTrue(ck.verify_image(out).ok)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "PLANNED")
            self.assertEqual(data["family"], "4560BO00S")
            self.assertEqual(len(data["symbols"]), 2)

    def test_rejection_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.bin"
            code = cal.main(["--image", str(BO00_CAL), "--set", "c_n_lim_min=7500", "--out", str(out)])
            self.assertEqual(code, 2)
            self.assertFalse(out.exists())

    def test_pops_needs_acknowledge_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.bin"
            base = ["--image", str(BO00_CAL), "--set", "c_t_min_pu=0.5", "--out", str(out)]
            self.assertEqual(cal.main(base), 2)
            self.assertEqual(cal.main(base + ["--acknowledge", "no-catalyst"]), 0)
            self.assertTrue(out.exists())

    def test_missing_out(self) -> None:
        self.assertEqual(cal.main(["--image", str(BO00_CAL), "--set", "c_vs_max=255"]), 2)

    def test_bad_set_syntax(self) -> None:
        self.assertEqual(cal.main(["--image", str(BO00_CAL), "--set", "c_vs_max"]), 2)

    def test_lo00_family_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.bin"
            code = cal.main(["--image", str(LO00_CAL), "--set", "c_vs_max=250", "--out", str(out)])
            self.assertEqual(code, 0)
            report = ck.verify_image(out)
            self.assertTrue(report.ok)
            self.assertEqual(report.family, "4570LO00S")
            self.assertEqual(out.read_bytes()[0x1944], 250)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

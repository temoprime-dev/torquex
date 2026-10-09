"""Проверки поддержки 4560BM00S (шаг 3: семейство доведено до полного набора ролей).

До шага 3 в семействе была разрешена только доказанная группа задержек ПХХ, а
структурно похожий блок 6x9 около 0x5F9A считался недоказанным. Теперь адрес
лимитера скорости и адрес группы лимитеров оборотов доказаны структурными
отпечатками по всем 32 стокам семьи; отпечатки воспроизводят адреса, известные
из первоисточников у 4560BO00S/4570LO00S (XDF) и 4560BN00S (XML). Метод, сами
отпечатки и сборка на всех стоках — ``tests/test_family_stock_proof.py`` и
``docs/FAMILY_CALIBRATION_EVIDENCE_RU.md``.
"""

from __future__ import annotations

from pathlib import Path
import struct
import unittest

from MS45.tools import calibration as cal
from MS45.tools import checksums as ck
from MS45.tools import policy as pl
from MS45.tools import stages
from MS45.tests.support import unknown_family_image  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SPDATEN = ROOT / "MS45" / "firmware_spdaten" / "cal"
FAMILY = "4560BM00S"
POPS_SYMBOLS = ("c_t_min_pu", "c_t_min_pu_vs", "c_t_min_pu_rng_l")
RPM_CANDIDATES = (
    "id_n_max_amt",
    "id_n_max_at",
    "id_n_max_h_amt",
    "id_n_max_h_at",
    "id_n_max_h_mt",
    "id_n_max_mt",
)


class TestBm00sLimitedSupport(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stocks = pl.stock_images(FAMILY)
        cls.policy = pl.load_policy()
        cls.family_policy = cls.policy.for_family(FAMILY)

    def test_all_32_family_stocks_are_valid_and_counted(self) -> None:
        self.assertEqual(len(self.stocks), 32)
        for name, image in self.stocks:
            with self.subTest(stock=name):
                self.assertEqual(len(image), ck.CAL_SIZE)
                self.assertEqual(ck.read_family(image), FAMILY)
                report = ck.verify_bytes(image)
                self.assertTrue(report.ok, f"{name}: {report.as_dict()}")

        for symbol in (*POPS_SYMBOLS, "c_t_min_pu_msr", "c_n_lim_min"):
            entry = self.family_policy.objects[symbol]
            self.assertEqual(entry.stock.samples, 32, symbol)
        objects = self.family_policy.objects
        # Группа доказана от 0x5C00 (второе слово — инвариант msr = 1.00 s);
        # см. tests/test_pops_delay_group.py и docs/FAMILY_CALIBRATION_EVIDENCE_RU.md §1.3.
        self.assertEqual(objects["c_t_min_pu"].address, 0x5C00)
        self.assertEqual(objects["c_t_min_pu_msr"].address, 0x5C02)
        self.assertEqual(objects["c_t_min_pu_rng_l"].address, 0x5C04)
        self.assertEqual(objects["c_t_min_pu_vs"].address, 0x5C06)
        self.assertEqual(
            (objects["c_t_min_pu"].stock.minimum, objects["c_t_min_pu"].stock.maximum),
            (0, 24),
        )
        self.assertEqual(
            (objects["c_t_min_pu_msr"].stock.minimum, objects["c_t_min_pu_msr"].stock.maximum),
            (100, 100),
        )
        self.assertEqual(
            (objects["c_t_min_pu_rng_l"].stock.minimum, objects["c_t_min_pu_rng_l"].stock.maximum),
            (0, 120),
        )
        self.assertEqual(
            (objects["c_t_min_pu_vs"].stock.minimum, objects["c_t_min_pu_vs"].stock.maximum),
            (18, 46),
        )
        self.assertEqual(
            (objects["c_n_lim_min"].stock.minimum, objects["c_n_lim_min"].stock.maximum),
            (7200, 7200),
        )

    def test_rpm_blocks_match_the_proven_layout(self) -> None:
        """Шесть блоков по 9 значений стоят в порядке XDF 456BO02S/457LO02S.

        Смещения от начала группы (+0x00 amt, +0x12 at, +0x24 h_amt, +0x36 h_at,
        +0x48 h_mt, +0x5A mt) и число значений проверены на 4560BO00S/4570LO00S,
        где адреса даёт XDF; у BM00S адрес группы найден структурным отпечатком
        по 32 стокам (tests/test_family_stock_proof.py).
        """
        offsets = {
            "id_n_max_amt": 0x00, "id_n_max_at": 0x12, "id_n_max_h_amt": 0x24,
            "id_n_max_h_at": 0x36, "id_n_max_h_mt": 0x48, "id_n_max_mt": 0x5A,
        }
        base = self.family_policy.objects["id_n_max_amt"].address
        for symbol, delta in offsets.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(self.family_policy.objects[symbol].address, base + delta)
                self.assertEqual(self.family_policy.objects[symbol].columns, 9)
        for name, image in self.stocks:
            with self.subTest(stock=name):
                blocks = {
                    symbol: [
                        int.from_bytes(image[base + delta + 2 * index : base + delta + 2 * index + 2], "big")
                        for index in range(9)
                    ]
                    for symbol, delta in offsets.items()
                }
                for symbol, values in blocks.items():
                    self.assertTrue(all(5000 <= value <= 7400 for value in values), (name, symbol))
                for gear in range(9):
                    self.assertGreaterEqual(blocks["id_n_max_h_mt"][gear], blocks["id_n_max_mt"][gear])
                    self.assertGreaterEqual(blocks["id_n_max_h_at"][gear], blocks["id_n_max_at"][gear])
                    self.assertGreaterEqual(blocks["id_n_max_h_amt"][gear], blocks["id_n_max_amt"][gear])
                # Группа изолирована: соседние слова не похожи на rpm той же сетки.
                before = int.from_bytes(image[base - 2 : base], "big")
                after = int.from_bytes(image[base + 0x5A + 18 : base + 0x5A + 20], "big")
                self.assertFalse(5000 <= before <= 7400, (name, before))
                self.assertFalse(5000 <= after <= 7400, (name, after))

    def test_roles_and_packs_cover_limits_and_pops(self) -> None:
        """С шага 3 разрешены лимиты (vmax + три лимитера) и задержки ПХХ; с
        доказательства кластера зажигания (test_family_ignition_proof) — карта RON98."""
        expected_roles = {"vmax", "rev_limit_mt", "rev_limit_at", "rev_limit_amt", "pops_delay",
                          "ignition"}
        self.assertEqual(set(self.family_policy.roles), expected_roles)
        self.assertEqual(set(self.family_policy.packs), {"vmax", "rev_limit", "pops", "ignition"})
        for symbol in (*POPS_SYMBOLS, "c_t_min_pu_msr", "c_n_lim_min", "c_vs_max", *RPM_CANDIDATES,
                       "ip_iga_bas_ron_98__n__maf"):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, self.family_policy.objects)
        # Топливо/ВАНос в семействе не доказаны: символов нет.
        for symbol in ("ip_lamb_fl__n", "ip_cam_sp_2_in_pl__n__maf_ivvt"):
            with self.subTest(symbol=symbol):
                self.assertNotIn(symbol, self.family_policy.objects)
                with self.assertRaises(pl.PolicyError) as ctx:
                    self.family_policy.entry(symbol)
                self.assertEqual(ctx.exception.code, pl.CODE_SYMBOL_NOT_ALLOWED)

    def test_core_writes_limits_within_planned_cells(self) -> None:
        image = self.stocks[0][1]
        plan = cal.plan_changes(
            image,
            [
                {"symbol": "c_vs_max", "values": [250]},
                {"symbol": "id_n_max_mt", "values": [6800] * 9},
                {"symbol": "id_n_max_h_mt", "values": [6800] * 9},
            ],
            policy=self.policy,
        )
        self.assertEqual(plan.status, "PLANNED", [finding.code for finding in plan.findings])
        cells = {
            offset
            for cell in plan.planned_cells
            for offset in range(cell.image_offset, cell.image_offset + cell.size_bytes)
        }
        self.assertIn(self.family_policy.objects["c_vs_max"].address, cells)
        base = self.family_policy.objects["id_n_max_mt"].address
        self.assertTrue(all(base + 2 * index in cells for index in range(9)))
        for symbol in ("c_n_lim_min", "c_t_min_pu_msr"):
            with self.subTest(symbol=symbol):
                rejected = cal.plan_changes(
                    image, [{"symbol": symbol, "values": [7000]}], policy=self.policy
                )
                self.assertEqual(rejected.status, "REJECTED")
                self.assertEqual([finding.code for finding in rejected.findings], [pl.CODE_READ_ONLY])

    def test_protection_objects_are_read_only(self) -> None:
        for symbol in ("c_n_lim_min", "c_t_min_pu_msr", "val_mo3_cal_cks_high", "val_mo3_cal_cks_low"):
            with self.subTest(symbol=symbol):
                entry = self.family_policy.objects[symbol]
                self.assertTrue(entry.read_only)
                with self.assertRaises(pl.PolicyError) as ctx:
                    self.family_policy.entry(symbol)
                self.assertEqual(ctx.exception.code, pl.CODE_READ_ONLY)

    def test_vmax_and_all_transmission_limits_build_on_all_stocks(self) -> None:
        """Все 32 стока: vmax + лимитеры mt/at/amt собираются, CRC верна."""
        requests = (
            ({"kind": "vmax", "value": 250}, "c_vs_max"),
            ({"kind": "rev_limit", "value": 6700, "transmission": "mt", "acknowledge_risk": True}, "id_n_max_mt"),
            ({"kind": "rev_limit", "value": 6700, "transmission": "at", "acknowledge_risk": True}, "id_n_max_at"),
            ({"kind": "rev_limit", "value": 6700, "transmission": "amt", "acknowledge_risk": True}, "id_n_max_amt"),
        )
        for name, image in self.stocks:
            for intent, symbol in requests:
                with self.subTest(stock=name, intent=intent["kind"], transmission=intent.get("transmission")):
                    result = stages.build(
                        "stage1", image, {"acknowledge_legality": True, "intents": [intent]},
                        policy=self.policy, source_name=name,
                    )
                    self.assertEqual(result.status, "BUILT", result.findings)
                    self.assertEqual([item.id for item in result.pending], [])
                    self.assertIsNotNone(result.plan)
                    self.assertIsNotNone(result.result)
                    assert result.plan is not None and result.result is not None
                    self.assertTrue(ck.verify_bytes(result.result.image).ok)
                    planned = {
                        offset
                        for cell in result.plan.planned_cells
                        for offset in range(cell.image_offset, cell.image_offset + cell.size_bytes)
                    }
                    allowed = planned | set(result.plan.checksum_cells)
                    changed = {
                        offset
                        for offset, (before, after) in enumerate(zip(image, result.result.image))
                        if before != after
                    }
                    self.assertLessEqual(changed, allowed)
                    self.assertIn(self.family_policy.objects[symbol].address, planned)

    def test_pops_requires_every_acknowledgement_in_order(self) -> None:
        """Три подтверждения по очереди: правовая справка -> катализатор -> температура."""
        image = self.stocks[0][1]
        intent = {"kind": "pops", "delay_s": 0.5}
        codes = lambda result: [f["code"] for f in result.findings]
        result = stages.build("stage1", image, {"intents": [intent]}, policy=self.policy)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("ACKNOWLEDGE_LEGALITY_REQUIRED", codes(result))

        request = {"acknowledge_legality": True, "intents": [intent]}
        result = stages.build("stage1", image, request, policy=self.policy)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("ACKNOWLEDGE_REQUIRED", codes(result))
        self.assertTrue(any("no_catalyst" in f["message"] for f in result.findings))

        request = {"acknowledge_legality": True, "no_catalyst": True,
                   "intents": [intent]}
        result = stages.build("stage1", image, request, policy=self.policy)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("ACKNOWLEDGE_REQUIRED", codes(result))
        self.assertTrue(any("acknowledge_temperature" in f["message"] for f in result.findings))

    def test_aggressive_pops_profile_is_refused(self) -> None:
        image = self.stocks[0][1]
        result = stages.build(
            "stage1",
            image,
            {"acknowledge_legality": True, "no_catalyst": True,
             "acknowledge_temperature": True,
             "intents": [{"kind": "pops", "delay_s": 0.5, "profile": "aggressive"}]},
            policy=self.policy,
        )
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("POPS_PROFILE_FORBIDDEN", [finding["code"] for finding in result.findings])
        self.assertIsNone(result.result)

    def test_limits_and_pops_build_in_one_stage(self) -> None:
        image = self.stocks[0][1]
        request = {
            "acknowledge_legality": True,
            "no_catalyst": True,
            "acknowledge_temperature": True,
            "intents": [
                {"kind": "pops", "delay_s": 0.5},
                {"kind": "rev_limit", "value": 6700, "transmission": "mt", "acknowledge_risk": True},
            ],
        }
        result = stages.build("stage1", image, request, policy=self.policy)
        self.assertEqual(result.status, "BUILT", result.findings)
        self.assertEqual({item.id for item in result.written_packages}, {"limits", "pops"})
        self.assertEqual([item.id for item in result.pending], [])
        assert result.plan is not None
        symbols = {item.symbol for item in result.plan.symbols}
        self.assertTrue(set(POPS_SYMBOLS) <= symbols)
        self.assertTrue({"id_n_max_mt", "id_n_max_h_mt"} <= symbols)

    def test_pops_writes_only_three_delays_and_recomputes_crc(self) -> None:
        """Запись в чужую группу невозможна: на всех 32 стоках сборка хлопков
        меняет ровно три слова доказанной группы (0x5C00/0x5C04/0x5C06) и
        контрольные суммы; все остальные байты — байт в байт как в стоке,
        включая слово msr (0x5C02) и хвостовые слова 0x5C08..0x5C0E."""
        request = {
            "acknowledge_legality": True,
            "no_catalyst": True,
            "acknowledge_temperature": True,
            "intents": [{"kind": "pops", "delay_s": 0.5, "profile": "light"}],
        }
        expected_cells = {0x5C00, 0x5C01, 0x5C04, 0x5C05, 0x5C06, 0x5C07}
        for name, image in self.stocks:
            with self.subTest(stock=name):
                result = stages.build("stage1", image, request, policy=self.policy, source_name=name)
                self.assertEqual(result.status, "BUILT", result.findings)
                self.assertEqual({item.id for item in result.written_packages}, {"pops"})
                self.assertIsNotNone(result.plan)
                self.assertIsNotNone(result.result)
                assert result.plan is not None and result.result is not None
                self.assertEqual({symbol.symbol for symbol in result.plan.symbols}, set(POPS_SYMBOLS))
                self.assertTrue(ck.verify_bytes(result.result.image).ok)

                planned = {
                    offset
                    for cell in result.plan.planned_cells
                    for offset in range(cell.image_offset, cell.image_offset + cell.size_bytes)
                }
                self.assertEqual(planned, expected_cells)
                changed = {
                    offset
                    for offset, (before, after) in enumerate(zip(image, result.result.image))
                    if before != after
                }
                allowed = planned | set(result.plan.checksum_cells)
                self.assertTrue(changed)
                self.assertLessEqual(changed, allowed)
                # Каждое из трёх слов получило запрошенное значение
                # (0.5 с = сырое 50; старшие байты стоковых слов уже 0x00,
                # поэтому «изменённых» байтов может быть меньше плановых).
                for addr in (0x5C00, 0x5C04, 0x5C06):
                    self.assertEqual(struct.unpack_from(">H", result.result.image, addr)[0], 50)
                self.assertEqual(len(result.result.image), ck.CAL_SIZE)
                # Ни одно слово вне доказанной группы не тронуто.
                for offset in range(0x5C02, 0x5C10):
                    if offset not in allowed:
                        self.assertEqual(result.result.image[offset], image[offset], hex(offset))

    def test_unknown_family_id_is_refused(self) -> None:
        """Чужой (не из десяти целевых) идентификатор семейства по-прежнему закрыт.

        Все десять семейств MS45 поддержаны с шага 3, поэтому «чужой» — это
        синтезированный идентификатор (MS45/tests/support.py).
        """
        image = unknown_family_image(self.stocks[0][1])
        self.assertEqual(ck.read_family(image), "4560BZ00S")
        plan = cal.plan_changes(
            image, [{"symbol": "c_t_min_pu", "values": [0.5]}], policy=self.policy
        )
        self.assertEqual([finding.code for finding in plan.findings], [pl.CODE_CAL_VERSION_UNSUPPORTED])
        result = stages.build(
            "stage1", image,
            {"acknowledge_legality": True, "intents": [{"kind": "pops", "delay_s": 0.5}]},
            policy=self.policy,
        )
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("CAL_VERSION_UNSUPPORTED", [finding["code"] for finding in result.findings])

    def test_all_ten_target_families_are_supported(self) -> None:
        self.assertEqual(len(pl.SUPPORTED_FAMILIES), 10)
        self.assertIn(FAMILY, pl.SUPPORTED_FAMILIES)

if __name__ == "__main__":
    unittest.main()

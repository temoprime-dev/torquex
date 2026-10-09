"""Тесты политики MS45 (``MS45/tools/policy.py``).

Политика — это allowlist: проверяется и её содержимое (адреса/формулы совпадают с
определением), и то, что она **отвергает** всё лишнее.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from MS45.tools import checksums as ck
from MS45.tools import policy as pl

ROOT = Path(__file__).resolve().parents[2]
MS45 = ROOT / "MS45"


class TestPolicyContent(unittest.TestCase):
    """Содержимое политики: семейства, роли, границы, защищённые символы."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = pl.load_policy()

    def test_families(self) -> None:
        self.assertEqual(self.policy.version, pl.POLICY_VERSION)
        self.assertEqual(self.policy.family, "MS45")
        self.assertEqual(set(self.policy.software), set(pl.SUPPORTED_FAMILIES))

    def test_required_roles_present(self) -> None:
        for family in pl.SUPPORTED_FAMILIES:
            fp = self.policy.for_family(family)
            for role in pl.REQUIRED_ROLES_BY_FAMILY.get(family, pl.REQUIRED_ROLES):
                self.assertIn(role, fp.roles, f"{family}:{role}")

    def test_every_entry_matches_definition(self) -> None:
        """Каждый символ политики совпадает с первоисточником семейства (XDF или XML)."""
        for family in pl.SUPPORTED_FAMILIES:
            for symbol, entry in self.policy.for_family(family).objects.items():
                obj = pl.family_symbol(family, symbol)
                self.assertEqual(entry.address, obj.address, f"{family}:{symbol}")
                self.assertEqual(entry.element_size_bits, obj.element_size_bits, f"{family}:{symbol}")
                self.assertEqual((entry.rows, entry.columns), (obj.rows, obj.columns), f"{family}:{symbol}")
                self.assertAlmostEqual(entry.scale_a, obj.scale_a, places=12, msg=f"{family}:{symbol}")
                self.assertAlmostEqual(entry.scale_b, obj.scale_b, places=12, msg=f"{family}:{symbol}")

    def test_definitions_sources(self) -> None:
        """Источник определений: XDF у BO/LO, XML сообщества у BN."""
        for family in pl.SUPPORTED_FAMILIES:
            source = pl.definition_path(family)
            if family in pl.XML_FAMILIES:
                self.assertIn("MS45 ECU Definitions.xml", source)
                self.assertTrue(pl.family_symbol(family, "c_vs_max").source.startswith("xml:"))
            elif family in pl.STOCK_DERIVED_FAMILIES:
                self.assertTrue(source.endswith("stock_derived_maps.json"), source)
                self.assertTrue(pl.family_symbol(family, "c_t_min_pu").source.startswith("stock:"))
            else:
                self.assertTrue(source.endswith(".xdf"), source)
                self.assertTrue(pl.family_symbol(family, "c_vs_max").source.startswith("xdf:"))

    def test_protected_symbols_are_read_only(self) -> None:
        for family in pl.SUPPORTED_FAMILIES:
            fp = self.policy.for_family(family)
            for symbol in pl.REQUIRED_READ_ONLY[family]:
                entry = fp.objects[symbol]
                self.assertTrue(entry.read_only, f"{family}:{symbol}")
                with self.assertRaises(pl.PolicyError) as ctx:
                    fp.entry(symbol)
                self.assertEqual(ctx.exception.code, pl.CODE_READ_ONLY)

    def test_unknown_symbol_refused(self) -> None:
        fp = self.policy.for_family("4560BO00S")
        # ip_iga_bas_ron_98__n__maf и флаги слоя «без катализатора» с задания 3
        # разрешены; проверяем запрещённое (битовая маска OBD намеренно вне слоя)
        for symbol in ("ip_fac_tq_req_driv_mon", "c_obd_state_2", "c_tq_max_acc_1", "c_n_max_cmd"):
            with self.assertRaises(pl.PolicyError) as ctx:
                fp.entry(symbol)
            self.assertEqual(ctx.exception.code, pl.CODE_SYMBOL_NOT_ALLOWED, symbol)

    def test_unsupported_family_refused(self) -> None:
        # С шага 3 поддержаны все десять целевых семейств; отказ проверяется на
        # чужом (синтезированном) и служебном идентификаторах.
        for family in ("4560BZ00S", "4570LO02S", None):
            with self.assertRaises(pl.PolicyError) as ctx:
                self.policy.for_family(family)
            self.assertEqual(ctx.exception.code, pl.CODE_CAL_VERSION_UNSUPPORTED)

    def test_no_checksum_or_signature_symbols_writable(self) -> None:
        """Суммы и подпись не могут быть в разрешённых ни у одного семейства."""
        for family in pl.SUPPORTED_FAMILIES:
            for symbol, entry in self.policy.for_family(family).objects.items():
                if entry.read_only:
                    continue
                self.assertNotIn(entry.address, (0x100, 0x101, 0x102, 0x103), symbol)
                self.assertFalse(0x170 <= entry.address <= 0x1B3, symbol)

    def test_pops_requires_acknowledgement(self) -> None:
        for family in pl.SUPPORTED_FAMILIES:
            fp = self.policy.for_family(family)
            for symbol in fp.packs["pops"]:
                self.assertEqual(fp.acknowledgement(symbol), "no-catalyst", f"{family}:{symbol}")

    def test_limits_are_meaningful(self) -> None:
        bo = self.policy.for_family("4560BO00S")
        self.assertEqual(bo.entry("c_vs_max").minimum, 100.0)
        self.assertEqual(bo.entry("c_vs_max").maximum, 255.0)
        for symbol in bo.roles["rev_limit_mt"]:
            self.assertEqual(bo.entry(symbol).minimum, 6000.0)
            self.assertEqual(bo.entry(symbol).maximum, 7000.0)
            self.assertEqual(bo.entry(symbol).max_delta_percent, 10.0)
        for symbol in bo.roles["pops_delay"]:
            self.assertEqual(bo.entry(symbol).maximum, 1.5)

    def test_stock_stats_from_data(self) -> None:
        bo = self.policy.for_family("4560BO00S")
        vmax = bo.entry("c_vs_max")
        self.assertEqual(vmax.stock.samples, 81)
        self.assertEqual((vmax.stock.minimum, vmax.stock.maximum), (206, 255))
        self.assertFalse(vmax.stock.uniform)
        lim = bo.objects["c_n_lim_min"]  # защищённый символ: только через objects
        self.assertTrue(lim.stock.uniform)
        self.assertEqual(lim.stock.minimum, 7200)


class TestStockGuard(unittest.TestCase):
    """Защита от чужой версии: стоковые файлы обязаны читаться в пределах стока.

    Если адреса политики верны только для части файлов семейства, этот тест падает
    на первом же файле — до того, как генератор запишет что-то не туда.
    """

    def test_every_stock_file_reads_within_measured_stock(self) -> None:
        policy = pl.load_policy()
        paths = (
            sorted((MS45 / "firmware_spdaten" / "cal").glob("*.bin"))
            + sorted((MS45 / "firmware").rglob("*.bin"))
            + sorted((MS45 / "firmware_dumps").glob("*.bin"))
        )
        checked = 0
        families: set[str] = set()
        for path in paths:
            try:
                cal, _ = ck.extract_cal(path.read_bytes())
            except ck.MS45Error:
                continue
            family = ck.read_family(cal)
            if family not in policy.software:
                continue
            families.add(family)
            for symbol, entry in policy.for_family(family).objects.items():
                size = entry.element_size_bits // 8
                raw = int.from_bytes(cal[entry.address : entry.address + size], "big")
                stock = entry.stock
                self.assertIsNotNone(stock.minimum, f"{path.name}:{symbol}: нет измеренного стока")
                self.assertLessEqual(stock.minimum, raw, f"{path.name}:{symbol}={raw} ниже стока")
                self.assertLessEqual(raw, stock.maximum, f"{path.name}:{symbol}={raw} выше стока")
                checked += 1
        self.assertEqual(families, set(policy.software))
        self.assertGreater(checked, 1000)


class TestQuantization(unittest.TestCase):
    """Квантование: округление к сырому коду и ошибка в процентах."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bo = pl.load_policy().for_family("4560BO00S")

    def test_exact_values(self) -> None:
        code, error = self.bo.entry("c_t_min_pu").quantize(0.5)
        self.assertEqual(code, 50)
        self.assertEqual(error, 0.0)
        code, error = self.bo.entry("c_vs_max").quantize(255)
        self.assertEqual((code, error), (255, 0.0))

    def test_rounding_reported(self) -> None:
        code, error = self.bo.entry("c_t_min_pu").quantize(0.505)
        self.assertEqual(code, 51)
        self.assertGreater(error, 0.0)
        self.assertLess(error, 1.0)

    def test_out_of_raw_range(self) -> None:
        """Значение, не выразимое в разрядности (700 с * 100 = 70 000 > 65535)."""
        with self.assertRaises(pl.PolicyError) as ctx:
            self.bo.entry("c_t_min_pu").quantize(700.0)
        self.assertEqual(ctx.exception.code, pl.CODE_VALUE_NOT_REPRESENTABLE)

    def test_policy_range_is_separate_from_representability(self) -> None:
        """Границы политики и разрядность — разные проверки."""
        entry = self.bo.entry("c_t_min_pu")
        self.assertEqual(entry.quantize(3.0)[0], 300)  # выразимо, но выйдет за 1.5 с
        self.assertTrue(entry.check_value(3.0))

    def test_check_value_reports_codes(self) -> None:
        entry = self.bo.entry("c_vs_max")
        self.assertEqual(entry.check_value(200), [])
        codes = [msg.split(":", 1)[0] for msg in entry.check_value(90)]
        self.assertEqual(codes, [pl.CODE_POLICY_RANGE_VIOLATION])
        # 400 км/ч: и вне допуска, и не выразимо в 8 битах (400 / 1.0 = 400 > 255).
        codes = [msg.split(":", 1)[0] for msg in entry.check_value(400)]
        self.assertEqual(codes, [pl.CODE_POLICY_RANGE_VIOLATION, pl.CODE_VALUE_NOT_REPRESENTABLE])

    def test_delta_reference(self) -> None:
        entry = self.bo.entry("id_n_max_mt")
        self.assertIsNotNone(entry.stock.minimum)
        self.assertEqual(entry.to_engineering(entry.stock.minimum), 6480.0)


class TestBuildAndValidate(unittest.TestCase):
    """Сборка политики из данных и её проверка."""

    def test_build_is_deterministic_and_matches_file(self) -> None:
        """Политика в репозитории — ровно то, что собирается из данных."""
        built = pl.build_policy()
        on_disk = json.loads((MS45 / "policies" / "stage_policy.json").read_text(encoding="utf-8"))
        self.assertEqual(built.as_dict(), on_disk)

    def test_build_bytes_match_file(self) -> None:
        """Сборка политики побайтово равна файлу в репозитории."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "policy.json"
            pl.write_policy(pl.build_policy(), target)
            self.assertEqual(
                target.read_bytes(),
                (MS45 / "policies" / "stage_policy.json").read_bytes(),
            )

    def test_written_policy_uses_lf(self) -> None:
        """Артефакт не зависит от ОС: переводы строк — LF, а не CRLF."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "policy.json"
            pl.write_policy(pl.build_policy(), target)
            self.assertNotIn(b"\r", target.read_bytes())

    def test_windows_style_paths_do_not_change_the_policy(self) -> None:
        """Simulated Windows: путь XML-первоисточника с ``\\`` не меняет артефакт."""
        real = pl.pathfmt.relative_posix
        windows_style = "definitions\\MS45 ECU Definitions.xml"

        def fake(path, root):  # noqa: ANN001 - повторяем сигнатуру
            text = real(path, root)
            return text.replace("/", "\\") if text.endswith(".xml") else text

        with mock.patch.object(pl.pathfmt, "relative_posix", side_effect=fake):
            built = pl.build_policy()
        # в объекте путь такой, каким его отдала ОС
        self.assertEqual(built.for_family("4560BN00S").definition_path, windows_style)
        # а в артефакте — всегда POSIX, поэтому файл не зависит от платформы
        text = json.dumps(built.as_dict(), ensure_ascii=False)
        self.assertNotIn("\\", text)
        self.assertIn("definitions/MS45 ECU Definitions.xml", text)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "policy.json"
            pl.write_policy(built, target)
            self.assertEqual(
                target.read_bytes(),
                (MS45 / "policies" / "stage_policy.json").read_bytes(),
            )

    def test_definition_paths_are_posix(self) -> None:
        policy = pl.load_policy()
        expected = {
            "4560BO00S": "definitions/Siemens_MS450_456BO02S_116K.xdf",
            "4570LO00S": "definitions/Siemens_MS451_457LO02S_116K.xdf",
            "4560BN00S": "definitions/MS45 ECU Definitions.xml",
        }
        for family, want in expected.items():
            got = policy.for_family(family).definition_path
            self.assertEqual(got, want)
            self.assertNotIn("\\", got)
        text = (MS45 / "policies" / "stage_policy.json").read_text(encoding="utf-8")
        self.assertNotIn("\\", text)

    def test_validate_ok(self) -> None:
        self.assertEqual(pl.validate_policy(pl.load_policy()), [])

    def test_validate_catches_wrong_address(self) -> None:
        policy = pl.load_policy()
        entry = policy.for_family("4560BO00S").objects["c_vs_max"]
        entry.address = 0x1234
        problems = pl.validate_policy(policy)
        self.assertTrue(any("адрес политики" in p for p in problems), problems)

    def test_validate_catches_bad_limits(self) -> None:
        policy = pl.load_policy()
        entry = policy.for_family("4560BO00S").objects["c_vs_max"]
        entry.minimum = 300.0
        problems = pl.validate_policy(policy)
        self.assertTrue(any("минимум больше максимума" in p for p in problems), problems)

    def test_validate_catches_limit_below_stock(self) -> None:
        policy = pl.load_policy()
        entry = policy.for_family("4560BO00S").objects["c_vs_max"]
        entry.maximum = 200.0
        problems = pl.validate_policy(policy)
        self.assertTrue(any("ниже стокового максимума" in p for p in problems), problems)

    def test_validate_catches_writable_protected(self) -> None:
        policy = pl.load_policy()
        entry = policy.for_family("4560BO00S").objects["c_n_lim_min"]
        entry.read_only = False
        problems = pl.validate_policy(policy)
        self.assertTrue(any("обязан быть read_only" in p for p in problems), problems)

    def test_write_and_reload_roundtrip(self) -> None:
        policy = pl.load_policy()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            pl.write_policy(policy, path)
            reloaded = pl.load_policy(path)
        self.assertEqual(reloaded.as_dict(), policy.as_dict())
        self.assertEqual(pl.validate_policy(reloaded), [])

    def test_load_requires_scaling_fields(self) -> None:
        data = json.loads((MS45 / "policies" / "stage_policy.json").read_text(encoding="utf-8"))
        del data["families"]["4560BO00S"]["objects"]["c_vs_max"]["scale_a"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(pl.PolicyError) as ctx:
                pl.load_policy(path)
        self.assertEqual(ctx.exception.code, pl.CODE_POLICY_INVALID)

    def test_cli_build_and_validate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "policy.json"
            self.assertEqual(pl.main(["--build", "--policy", str(target)]), 0)
            self.assertTrue(target.exists())
            self.assertEqual(pl.main(["--validate", "--policy", str(target)]), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

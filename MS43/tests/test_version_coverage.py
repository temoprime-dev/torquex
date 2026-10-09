"""Покрытие версий калибровки MS43 (задание 2, блок A).

Проверяется то, что задание требует доказать по каждому покрытому семейству:

* политика версии собирается **из определения этой версии** (официальный XDF
  MS4X) и **из стоков этой версии** — адреса 430069 на другие версии не
  переносятся (см. ``docs/VERSION_COVERAGE_RU.md``);
* сборка генератором на стоках версии проходит, CRC после записи сходятся,
  меняются только запланированные байты (пост-проверка ядра);
* чужая версия отклоняется, 64 КиБ чтения не пишутся, фичи патчлиста на
  версиях, где его basedata не проверена, не предлагаются;
* **байтовая сверка адресов с XDF**: каждый адрес политики читается из стока
  версии тем же определением, которым он собран, и сырые байты совпадают.

Тесты, которым нужны 64 КиБ определения версии (второй независимый источник
адресов), пропускаются, если файл не скачан: ``MS43/tools/fetch_definitions.py``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from MS43.tools import calibration as cal
from MS43.tools import checksum as ck
from MS43.tools import policy as pol
from MS43.tools import stages
from MS43.tools.xdf import load_definition

ROOT = Path(__file__).resolve().parents[2]
MS43 = ROOT / "MS43"
ARCHIVE = MS43 / "firmware_archive"

#: Полный сток 512 КиБ и чтение калибровки 64 КиБ на каждую покрытую версию.
STOCKS: dict[str, tuple[Path, Path]] = {
    "430056": (
        ARCHIVE / "E46_330i__c256954g_Ca430056_Stok.bin",
        ARCHIVE / "E53__c356750c_Ca430056_Stok_Cal.bin",
    ),
    "430037": (
        ARCHIVE / "E46_330i__b137954g_Ca430037_Stok.bin",
        ARCHIVE / "E46_330i__b137654_Ca430037_Stok.bin",
    ),
    "430055": (
        ARCHIVE / "E46_330i__C155G50G_Ca430055_Stok.bin",
        ARCHIVE / "E46_330i__C155652L_Ca430055_Stok_Cal.bin",
    ),
    "430066": (
        ARCHIVE / "E39_530__c6668528_Ca430066_Stok.bin",
        ARCHIVE / "E46_330i__c5666508_Ca430066_Stok_Cal.bin",
    ),
}

#: Символы для проверки адресов: скаляр, гистерезис ПХХ и таблица ДМРВ.
PROBE_SYMBOLS = ("c_vs_max_hys", "c_n_hys_max_puc", "id_maf_tab__v_maf")


def _codes(result) -> list[str]:
    """Коды находок результата генератора (``stages.build``)."""
    return [item["code"] for item in result.findings]


class RegistryTests(unittest.TestCase):
    def test_registry_covers_the_expected_versions(self) -> None:
        self.assertEqual(
            ("430069", "430056", "430037", "430055", "430066"),
            pol.covered_software(),
        )
        for version in pol.covered_software():
            path = pol.policy_path_for_version(version)
            self.assertIsNotNone(path, version)
            self.assertTrue(path.exists(), f"{version}: нет файла политики {path}")
            self.assertIsNotNone(pol.load_policy_for_version(version), version)

    def test_uncovered_version_has_no_policy(self) -> None:
        # 430064: в архиве только 64 КиБ чтения, полного образа нет — политики нет
        self.assertNotIn("430064", pol.covered_software())
        self.assertIsNone(pol.policy_path_for_version("430064"))
        self.assertIsNone(pol.load_policy_for_version("430064"))

    def test_only_default_policy_carries_patchlist_symbols(self) -> None:
        for version in pol.covered_software():
            policy = pol.load_policy_for_version(version)
            origins = {entry.origin for entry in policy.objects.values()}
            self.assertEqual({version}, set(policy.software), version)
            if version == pol.DEFAULT_SOFTWARE:
                self.assertEqual({"xdf", "patchlist"}, origins, version)
            else:
                self.assertEqual({"xdf"}, origins, version)
                self.assertGreater(len(policy.objects), 50, version)

    def test_each_non_default_policy_rebuilds_from_its_own_definition(self) -> None:
        """Файл политики версии обязан совпадать со сборкой из её определения."""
        for version in pol.covered_software():
            if version == pol.DEFAULT_SOFTWARE:
                continue
            committed = json.loads(
                pol.policy_path_for_version(version).read_text(encoding="utf-8")
            )
            built = pol.build_version_policy(version).as_dict()
            self.assertEqual(built, committed, version)

    def test_430037_has_no_torque_symbol_from_its_definition(self) -> None:
        # В определении 430037 нет ip_iga_tqr_amt__n__maf: объект не доказан —
        # в политику версии не входит (и это записано в описании).
        policy = pol.load_policy_for_version("430037")
        self.assertNotIn("ip_iga_tqr_amt__n__maf", policy.objects)
        self.assertIn("ip_iga_tqr_amt__n__maf", pol.load_policy_for_version("430056").objects)
        self.assertIn("ip_iga_tqr_amt__n__maf", policy.generated_note)


class AddressProofTests(unittest.TestCase):
    """Адреса политики версии читаются из её стока тем же определением."""

    def test_policy_addresses_match_the_version_definition_bytes(self) -> None:
        for version, (full, _cal64) in STOCKS.items():
            image = full.read_bytes()
            self.assertEqual(0x80000, len(image), full.name)
            definition = load_definition(pol.VERSION_DEFINITIONS[version][0])
            policy = pol.load_policy_for_version(version)
            for symbol in PROBE_SYMBOLS:
                entry = policy.objects[symbol]
                obj = definition.get(symbol)
                # адрес в политике — абсолютное смещение в образе 512 КиБ
                self.assertEqual(
                    definition.base_offset + obj.address, entry.address, f"{version}:{symbol}"
                )
                # и он же — то место, откуда определение читает сам объект
                through_definition = definition.read_raw(image, obj, 0)
                direct = int.from_bytes(
                    image[entry.address : entry.address + obj.element_size_bytes], "little"
                )
                self.assertEqual(through_definition, direct, f"{version}:{symbol}")

    def test_addresses_agree_with_the_64k_definition_of_the_same_version(self) -> None:
        """512 КиБ адрес минус BASEOFFSET == адрес 64 КиБ определения.

        Два официальных определения версии сгенерированы отдельно и обязаны
        указывать на один и тот же объект: 274 сверки на пять версий.
        """
        checked = 0
        for version in pol.covered_software():
            xdf_512, xdf_64 = pol.VERSION_DEFINITIONS[version]
            self.assertTrue(xdf_64.exists(), f"{version}: нет определения {xdf_64.name}")
            d5, d6 = load_definition(xdf_512), load_definition(xdf_64)
            self.assertEqual(0, d6.base_offset, f"{version}: у 64 КиБ определения база не 0")
            for symbol, entry in pol.load_policy_for_version(version).objects.items():
                if entry.origin != "xdf":
                    continue
                obj64 = (d6.objects.get(symbol) or [None])[entry.xdf_index or 0]
                self.assertIsNotNone(obj64, f"{version}:{symbol}: нет в 64 КиБ определении")
                self.assertEqual(
                    entry.address, d6.base_offset + obj64.address + 0x70000, f"{version}:{symbol}"
                )
                checked += 1
        expected = sum(
            len([e for e in pol.load_policy_for_version(v).objects.values() if e.origin == "xdf"])
            for v in pol.covered_software()
        )
        self.assertEqual(expected, checked, "сверены обязаны быть все XDF-символы всех версий")

    def test_64k_stock_reads_at_the_policy_offset(self) -> None:
        """В 64 КиБ чтении версии объект лежит по адресу политики минус 0x70000."""
        for version, (_full, cal64) in STOCKS.items():
            image = cal64.read_bytes()
            self.assertEqual(0x10000, len(image), cal64.name)
            self.assertEqual(version, ck.read_cal_version(image), cal64.name)
            definition = load_definition(pol.VERSION_DEFINITIONS[version][0])
            policy = pol.load_policy_for_version(version)
            for symbol in PROBE_SYMBOLS:
                entry = policy.objects[symbol]
                obj = definition.get(symbol)
                shift = entry.address - 0x70000
                direct = int.from_bytes(
                    image[shift : shift + obj.element_size_bytes], "little"
                )
                through_definition = definition.read_raw(image, obj, 0, base_offset=0)
                self.assertEqual(through_definition, direct, f"{version}:{symbol}")


class StockEnumerationTests(unittest.TestCase):
    """Стоки версии перечисляются без повторов (ревью 2: глоб на Windows).

    На регистронезависимой файловой системе ``glob("*.bin")`` и ``glob("*.Bin")``
    возвращают одни и те же файлы: счётчики стоков удваивались (у 430056 было 20
    образов вместо 10), и регенерация политики на Windows давала другие числа.
    Перечисление идёт через ``patches.bin_files`` (фильтр ``suffix.lower()`` +
    дедупликация нормализованного пути).
    """

    #: Ожидаемое число стоков каждой версии (не зависит от ОС).
    EXPECTED = {"430069": 19, "430056": 10, "430037": 11, "430055": 3, "430066": 5}

    def test_every_stock_file_is_listed_exactly_once(self) -> None:
        # независимый подсчёт: идём по каталогам и берём suffix.lower(), как это
        # делает сам код, но результат собираем в множества имён
        expected: dict[str, set[str]] = {}
        for directory in (pol.FIRMWARE_DIR, pol.ARCHIVE_DIR):
            self.assertTrue(directory.is_dir(), directory)
            for path in sorted(directory.iterdir()):
                if not path.is_file() or path.suffix.lower() != ".bin":
                    continue
                version = ck.read_cal_version(path.read_bytes())
                expected.setdefault(version, set()).add(path.name)
        for version, names in self.EXPECTED.items():
            listed = [name for name, _data, _base in pol.version_stock_images(version)]
            self.assertEqual(len(listed), len(set(listed)), f"{version}: повторы {listed}")
            self.assertEqual(names, len(listed), f"{version}: уникальных файлов {names}")
            self.assertEqual(expected[version], set(listed), version)

    def test_policies_count_exactly_the_enumerated_stocks(self) -> None:
        """Счётчик ``samples`` в политике версии — это число её стоков."""
        for version in pol.covered_software():
            policy = pol.load_policy_for_version(version)
            if version == pol.DEFAULT_SOFTWARE:
                # 430069 собирается из 18 полных стоков `MS43/firmware`;
                # 19-й образ версии — чтение калибровки 64 КиБ, в границы не
                # входит и образцом стока не считается.
                full = [n for n, data, _b in pol.version_stock_images(version) if len(data) == 0x80000]
                self.assertEqual(18, len(full))
                self.assertEqual({18}, {e.stock.samples for e in policy.objects.values() if e.stock})
                continue
            images = pol.version_stock_images(version)
            self.assertEqual(self.EXPECTED[version], len(images), version)
            samples = {entry.stock.samples for entry in policy.objects.values() if entry.stock}
            self.assertEqual({len(images)}, samples, version)

    def test_windows_style_duplicate_globs_are_deduped(self) -> None:
        """Старое перечисление (два глоба) + семантика Windows == новое."""
        from MS43.tools import patches as pt

        windows_key = lambda path: str(path).lower()  # noqa: E731 — семантика Windows
        for directory in (pol.FIRMWARE_DIR, pol.ARCHIVE_DIR):
            legacy = sorted(directory.glob("*.bin")) + sorted(directory.glob("*.Bin"))
            doubled = legacy + legacy  # так выглядело на регистронезависимой ФС
            now = [path.name for path in pt.bin_files(directory, key=windows_key)]
            before = [path.name for path in pt.unique_paths(doubled, key=windows_key)]
            self.assertEqual(sorted(now), sorted(before), directory)
            # и повторов внутри нет ни при какой нормализации
            self.assertEqual(len(now), len(set(now)), directory)

    def test_bin_files_sees_upper_case_suffix_once(self) -> None:
        from MS43.tools import patches as pt

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("first.bin", "second.Bin", "notes.txt", "third.BIN"):
                (root / name).write_bytes(b"x")
            names = [path.name for path in pt.bin_files(root)]
            self.assertEqual(["first.bin", "second.Bin", "third.BIN"], names)
            # Windows-семантика на регистронезависимой ФС: ничего не теряется
            lowered = [path.name for path in pt.bin_files(root, key=lambda p: str(p).lower())]
            self.assertEqual(3, len(lowered))

    def test_case_insensitive_normalization_does_not_change_counts(self) -> None:
        """С эмуляцией Windows (normcase → нижний регистр) числа те же."""
        import os
        from unittest import mock

        for version, expected in self.EXPECTED.items():
            with mock.patch.object(os.path, "normcase", new=lambda path: str(path).lower()):
                with mock.patch.object(os.path, "realpath", new=lambda path: str(path)):
                    images = pol.version_stock_images(version)
            self.assertEqual(expected, len(images), version)


class ContentAnchorTests(unittest.TestCase):
    """Адрес подтверждён **содержимым**, а не только определением версии.

    Часть таблиц MS43 — физические константы модели (порог педали полной
    нагрузки, задание VANOS на входе, отсечки по передачам, угол ПХХ): при смене
    версии ПО их байты не меняются, меняется только место. Поэтому эталон 430069
    (сток E46 330i M54B30 EU4, политика которого прошла ревью) обязан лежать в
    стоке версии **ровно по адресу её политики**. Если бы адрес версии был взят
    неверно, байты не совпали бы: у 430069 на чужих адресах лежат другие данные
    (`c_n_hys_max_puc` — 07/01/00/78 против 0F, см. `CALIBRATION_RU.md` §5a).
    """

    #: «Модельно-стабильные» таблицы (имена — из определений всех версий).
    ANCHORS = (
        "id_pvs_fl__n",
        "ip_cam_sp_tco_2_in_fl__n",
        "id_n_max_mt__gear",
        "ip_iga_puc__n__tco",
    )
    REFERENCE = MS43 / "firmware" / "Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin"

    def test_stable_tables_match_the_reference_by_content(self) -> None:
        reference = self.REFERENCE.read_bytes()
        ref_definition = load_definition(pol.VERSION_DEFINITIONS["430069"][0])
        ref_policy = pol.load_policy_for_version("430069")
        for version in pol.covered_software():
            definition = load_definition(pol.VERSION_DEFINITIONS[version][0])
            policy = pol.load_policy_for_version(version)
            stocks = pol.version_stock_images(version)
            for symbol in self.ANCHORS:
                self.assertIn(symbol, policy.objects, f"{version}: нет {symbol}")
                entry = policy.objects[symbol]
                ref_entry = ref_policy.objects[symbol]

                def _object(xdf, name, index):
                    found = xdf.objects.get(name)
                    if found is None:
                        return None
                    return found[index or 0] if isinstance(found, list) else found

                obj = _object(definition, symbol, entry.xdf_index)
                self.assertIsNotNone(obj, f"{version}:{symbol}: нет в определении")
                ref_obj = _object(ref_definition, symbol, ref_entry.xdf_index)
                self.assertIsNotNone(ref_obj, f"430069:{symbol}: нет в определении")
                size = obj.element_size_bytes * obj.rows * obj.columns
                ref_size = ref_obj.element_size_bytes * ref_obj.rows * ref_obj.columns
                self.assertEqual(size, ref_size, f"{version}:{symbol}: разные размеры")
                expected = reference[ref_entry.address : ref_entry.address + size]
                matches = 0
                for _name, data, base in stocks:
                    # 512 КиБ образ: смещение = абсолютный адрес; 64 КиБ чтение
                    # калибровки: калибровка начинается с нуля.
                    shift = 0 if base is None else 0x70000
                    address = entry.address - shift
                    if data[address : address + size] == expected:
                        matches += 1
                self.assertGreaterEqual(
                    matches,
                    1,
                    f"{version}:{symbol}: ни один сток версии не содержит байты "
                    f"эталона 430069 по адресу политики",
                )


class PatchlistScopeTests(unittest.TestCase):
    """Патчлист — только 430069: и символами, и намерениями, и на плане.

    Патчлист сообщества правит **программную** область (`0x00000..0x6FFFF`), а
    его basedata снята с 18 стоков 430069. Поэтому: в политиках других версий
    нет ни одного символа патчлиста, любой «фичевый» интент на них отклоняется
    `PATCHLIST_FEATURE_UNSUPPORTED`, а невыполнимые генератором фичи честно
    отклоняются и на 430069 (`PATCHLIST_FEATURE_NOT_IMPLEMENTED`).
    """

    def test_non_default_policies_have_no_patchlist_symbols(self) -> None:
        from MS43.tools import patches as pt

        patch_symbols = {item.title for item in pt.load_catalog().symbols}
        self.assertGreater(len(patch_symbols), 30)  # каталог на месте, не «пусто»
        for version in pol.covered_software():
            policy = pol.load_policy_for_version(version)
            if version == pol.DEFAULT_SOFTWARE:
                self.assertTrue(patch_symbols & set(policy.objects), version)
                continue
            # Читаемые оси карт, одноимённые символам патчлиста, допустимы:
            # они не пишутся. Запрещены именно записываемые символы патчлиста.
            writable_patch = {
                symbol for symbol in patch_symbols & set(policy.objects)
                if not policy.objects[symbol].read_only
            }
            self.assertEqual(set(), writable_patch, version)
            for symbol, entry in policy.objects.items():
                self.assertIsNone(entry.requires_patch, f"{version}:{symbol}")
                self.assertEqual("xdf", entry.origin, f"{version}:{symbol}")

    def test_every_patchlist_intent_is_refused_on_other_versions(self) -> None:
        for version in ("430056", "430037", "430055", "430066"):
            image = STOCKS[version][0].read_bytes()
            for kind in stages.PATCHLIST_INTENTS:
                result = stages.build(
                    "stage1",
                    image,
                    {"stage": "stage1", "intents": [{"id": "f1", "kind": kind, "rpm": 4000}]},
                )
                codes = _codes(result)
                self.assertIn("PATCHLIST_FEATURE_UNSUPPORTED", codes, f"{version}:{kind}")
                self.assertNotIn("INTENT_UNKNOWN", codes, f"{version}:{kind}")
                self.assertEqual("REJECTED", result.status, f"{version}:{kind}")
            # ни одной фичи патчлиста в доступных пакетах этой версии
            self.assertNotIn("features", result.available_packages, version)

    def test_unimplemented_patchlist_intents_are_refused_on_430069(self) -> None:
        image = STOCKS["430056"][0].read_bytes()
        reference = (MS43 / "firmware" / "Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin").read_bytes()
        for kind in sorted(stages.PATCHLIST_INTENTS):
            if kind in stages.PATCHLIST_IMPLEMENTED:
                continue
            result = stages.build(
                "stage1",
                reference,
                {"stage": "stage1", "intents": [{"id": "f1", "kind": kind}]},
            )
            self.assertIn("PATCHLIST_FEATURE_NOT_IMPLEMENTED", _codes(result), kind)
            features = next(p for p in result.packages if p.id == "features")
            self.assertEqual("rejected", features.status, kind)
            self.assertNotIn("INTENT_UNKNOWN", _codes(result), kind)
        # реализованные на 430069 фичи пишутся, а не отказывают
        for kind in sorted(stages.PATCHLIST_IMPLEMENTED):
            result = stages.build(
                "stage1",
                reference,
                {"stage": "stage1", "intents": [{"id": "f1", "kind": kind, "rpm": 4000}]},
            )
            self.assertNotIn("PATCHLIST_FEATURE_NOT_IMPLEMENTED", _codes(result), kind)
        self.assertEqual(0x80000, len(image))  # сток версии на месте (контроль)

    def test_calibration_plan_refuses_patches_on_other_versions(self) -> None:
        from MS43.tools import patches as pt

        title = "[PATCH] Launch Control & Rolling Anti Lag"
        for version in ("430056", "430037", "430055", "430066"):
            image = STOCKS[version][0].read_bytes()
            plan = cal.plan_changes(image, [], patches=(title,))
            self.assertEqual("REJECTED", plan.status, version)
            self.assertIn("PATCHLIST_FEATURE_UNSUPPORTED", [f.code for f in plan.findings], version)
            with self.assertRaises(cal.PatchError):
                cal.apply_plan(image, plan)
        # на 430069 тот же план строится (патчлист — его)
        supported = (MS43 / "firmware" / "Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin").read_bytes()
        plan = cal.plan_changes(supported, [], patches=(title,))
        self.assertEqual("PLANNED", plan.status, [f.message for f in plan.findings])
        self.assertEqual(("430069",), pt.supported_software())


class SeedStockTests(unittest.TestCase):
    """Стоки версии: значения в границах политики, ДМРВ монотонна."""

    def test_every_stock_of_every_covered_version_is_inside_the_bounds(self) -> None:
        for version in pol.covered_software():
            definition = load_definition(pol.VERSION_DEFINITIONS[version][0])
            policy = pol.load_policy_for_version(version)
            stocks = pol.version_stock_images(version)
            self.assertTrue(
                any(len(data) == 0x80000 for _n, data, _b in stocks),
                f"{version}: нет полного стока",
            )
            for name, data, base in stocks:
                for symbol, entry in policy.objects.items():
                    if entry.origin != "xdf":
                        continue
                    obj = (definition.objects.get(symbol) or [None])[entry.xdf_index or 0]
                    if obj is None:
                        continue
                    values = definition.read_engineering(data, obj, base)
                    for value in values:
                        self.assertGreaterEqual(value, entry.minimum - 1e-6, f"{name}:{symbol}")
                        self.assertLessEqual(value, entry.maximum + 1e-6, f"{name}:{symbol}")
                    if entry.require_monotonic and obj.rows * obj.columns > 1:
                        self.assertTrue(
                            all(a <= b for a, b in zip(values, values[1:])),
                            f"{name}:{symbol}: кривая не монотонна",
                        )

    def test_version_policy_validation_passes(self) -> None:
        for version in pol.covered_software():
            definition = load_definition(pol.VERSION_DEFINITIONS[version][0])
            policy = pol.load_policy_for_version(version)
            warnings = pol.validate_policy(policy, definition)
            self.assertEqual([], warnings, version)


class BuildOnStockTests(unittest.TestCase):
    """Сборка генератором на стоках версии: файл, CRC, пост-проверка."""

    def _vmax_intent(self, version: str, image: bytes) -> dict:
        """Поднять ограничитель скорости на 5 км/ч и жёсткий порог на 10 км/ч."""
        policy = pol.load_policy_for_version(version)
        entry = policy.objects["c_vs_max_mt_1"]
        size = max(1, entry.element_size_bits // 8)
        raw = int.from_bytes(image[entry.address : entry.address + size], "little")
        current = entry.scale_a * raw + entry.scale_b
        return {
            "id": "v1",
            "kind": "vmax",
            "transmission": "manual",
            "value_kmh": min(entry.maximum, current + 5),
            "hard_add_kmh": 10.0,
            "acknowledge_risk": True,
        }

    def test_vmax_builds_on_every_covered_version(self) -> None:
        for version, (full, _cal64) in STOCKS.items():
            image = full.read_bytes()
            result = stages.build(
                "stage1",
                image,
                {
                    "stage": "stage1",
                    "customer": "тест",
                    "intents": [self._vmax_intent(version, image)],
                },
            )
            self.assertEqual("BUILT", result.status, f"{version}: {result.findings}")
            self.assertEqual(version, result.cal_version)
            self.assertEqual((version,), result.software)
            self.verify(result.result.image, version)
            # каждый записанный байт — ровно там, где его адресует политика версии
            policy = pol.load_policy_for_version(version)
            written = [item for item in result.plan.symbols if item.cells]
            self.assertTrue(written, version)
            for item in written:
                entry = policy.objects[item.symbol]
                step = max(1, entry.element_size_bits // 8)
                for cell in item.cells:
                    self.assertEqual(entry.address + cell.index * step, cell.address,
                                     f"{version}:{item.symbol}")

    def test_rpm_limit_and_oil_pump_gate_on_every_covered_version(self) -> None:
        for version, (full, _cal64) in STOCKS.items():
            image = full.read_bytes()
            intent = {
                "id": "r1",
                "kind": "rpm_limit",
                "transmission": "manual",
                "soft_rpm": 6800,
                "acknowledge_risk": True,
            }
            result = stages.build(
                "stage1", image, {"stage": "stage1", "intents": [intent]}
            )
            # 6800 > 6750 (порог масляного насоса) — нужен отдельный ack
            self.assertIn("ACKNOWLEDGE_OIL_PUMP_REQUIRED", _codes(result), version)
            granted = dict(intent, acknowledge_oil_pump=True)
            result = stages.build(
                "stage1", image, {"stage": "stage1", "intents": [granted]}
            )
            self.assertEqual("BUILT", result.status, f"{version}: {result.findings}")
            self.verify(result.result.image, version)

    def test_popcorn_writes_on_every_covered_version(self) -> None:
        profile = {
            "band": "light",
            "rpm_add": 400,
            "no_catalyst": "катализатор удалён, подтверждаю письменно",
            "thermal_watch": True,
            "road_validation_plan": "первые заезды с логгером, следим за EGT",
        }
        for version, (full, _cal64) in STOCKS.items():
            image = full.read_bytes()
            result = stages.build(
                "stage1",
                image,
                {"stage": "stage1", "acknowledge_legality": True,
                 "intents": [{"id": "p1", "kind": "popcorn", "band": "light"}]},
                popcorn_profile=profile,
            )
            self.assertEqual("BUILT", result.status, f"{version}: {result.findings}")
            written = {item.symbol for item in result.plan.symbols if item.cells}
            self.assertIn("c_n_hys_max_puc", written, version)
            self.assertIn("ip_iga_puc__n__tco", written, version)
            # хлопок обязан быть одинаковым при любом состоянии A/C
            self.assertIn("ip_iga_accin_puc__n__tco", written, version)
            self.verify(result.result.image, version)

    def test_popcorn_delay_is_written_on_every_covered_version(self) -> None:
        """Задержка ПХХ (timered pops, MS4X wiki) пишется во всех версиях."""
        profile = {
            "band": "medium",
            "rpm_add": 400,
            "angle": -6.0,
            "delay_seconds": 1.5,
            "no_catalyst": "катализатор удалён, подтверждаю письменно",
            "thermal_watch": True,
            "road_validation_plan": "первые заезды с логгером, следим за EGT",
            "minimum_rpm_guard": {"requested": [1600, 1344, 1216, 1152, 1088, 1024]},
        }
        for version, (full, _cal64) in STOCKS.items():
            image = full.read_bytes()
            result = stages.build(
                "stage1",
                image,
                {"stage": "stage1", "acknowledge_legality": True,
                 "intents": [{"id": "p1", "kind": "popcorn", "band": "medium"}]},
                popcorn_profile=profile,
            )
            self.assertEqual("BUILT", result.status, f"{version}: {result.findings}")
            written = {item.symbol for item in result.plan.symbols if item.cells}
            for symbol in stages.POPCORN_DELAY_SYMBOLS:
                self.assertIn(symbol, written, f"{version}:{symbol}")
            self.assertIn("ip_n_min_accin_puc__tco", written, version)
            self.verify(result.result.image, version)

    def test_popcorn_delay_outside_the_band_is_refused(self) -> None:
        image = STOCKS["430056"][0].read_bytes()
        profile = {
            "band": "light",
            "rpm_add": 400,
            "delay_seconds": 3.0,  # предел полосы light — 1.0 с
            "no_catalyst": "катализатор удалён, подтверждаю письменно",
            "thermal_watch": True,
            "road_validation_plan": "первые заезды с логгером, следим за EGT",
        }
        result = stages.build(
            "stage1",
            image,
            {"stage": "stage1", "acknowledge_legality": True,
                 "intents": [{"id": "p1", "kind": "popcorn", "band": "light"}]},
            popcorn_profile=profile,
        )
        self.assertEqual("REJECTED", result.status)
        self.assertIn("POPCORN_BAND_VIOLATION", _codes(result))

    def verify(self, image: bytes, version: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"{version}.bin"
            path.write_bytes(image)
            self.assertTrue(ck.verify_image(path).ok, version)


class RefusalTests(unittest.TestCase):
    def test_foreign_policy_does_not_write(self) -> None:
        """Политика одной версии не принимает образ другой версии."""
        image = STOCKS["430056"][0].read_bytes()
        foreign_policy = pol.load_policy_for_version("430069")
        plan = cal.plan_changes(
            image, [{"symbol": "c_vs_max_hys", "values": [6.0]}], policy=foreign_policy
        )
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("CAL_VERSION_UNSUPPORTED", [f.code for f in plan.findings])
        self.assertEqual((), plan.symbols)
        with self.assertRaises(cal.PatchError):
            cal.apply_plan(image, plan)

    def test_64k_reads_are_refused_even_for_covered_versions(self) -> None:
        for version, (_full, cal64) in STOCKS.items():
            image = cal64.read_bytes()
            plan = cal.plan_changes(image, [{"symbol": "c_vs_max_hys", "values": [6.0]}])
            self.assertEqual("REJECTED", plan.status, version)
            self.assertEqual(["IMAGE_SIZE"], [f.code for f in plan.findings], version)

    def test_430064_full_image_is_refused(self) -> None:
        """430064: полного стока нет — подменённая строка версии обязана отказать."""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "raw.bin"
            broken = bytearray(STOCKS["430056"][0].read_bytes())
            broken[0x70008:0x7000E] = b"430064"
            source.write_bytes(bytes(broken))
            fixed = Path(directory) / "v430064.bin"
            ck.repair_image(source, fixed)
            image = fixed.read_bytes()
            self.assertEqual("430064", cal.read_cal_version(image))
            plan = cal.plan_changes(image, [{"symbol": "c_vs_max_hys", "values": [6.0]}])
            self.assertEqual("REJECTED", plan.status)
            self.assertIn("CAL_VERSION_UNSUPPORTED", [f.code for f in plan.findings])
            self.assertEqual(0, plan.policy_version)
            self.assertEqual(pol.covered_software(), plan.software)

    def test_patchlist_features_are_refused_on_other_versions(self) -> None:
        for version in ("430056", "430037", "430055", "430066"):
            image = STOCKS[version][0].read_bytes()
            result = stages.build(
                "stage1",
                image,
                {"stage": "stage1", "intents": [{"id": "lc1", "kind": "launch_control", "rpm": 4000}]},
            )
            self.assertEqual("REJECTED", result.status, version)
            self.assertIn("PATCHLIST_FEATURE_UNSUPPORTED", _codes(result), version)
            self.assertNotIn("features", result.available_packages, version)

    def test_patchlist_module_refuses_other_versions(self) -> None:
        from MS43.tools import patches as pt

        self.assertEqual(("430069",), pt.supported_software())
        with self.assertRaises(pt.PatchError) as caught:
            pt.refuse_unsupported_version(STOCKS["430056"][0].read_bytes())
        self.assertIn("CAL_VERSION_UNSUPPORTED", str(caught.exception))


class NoteTests(unittest.TestCase):
    def test_note_prints_version_and_available_packages(self) -> None:
        image = STOCKS["430056"][0].read_bytes()
        policy = pol.load_policy_for_version("430056")
        entry = policy.objects["c_vs_max_mt_1"]
        raw = int.from_bytes(image[entry.address : entry.address + 1], "little")
        intent = {
            "id": "v1",
            "kind": "vmax",
            "transmission": "manual",
            "value_kmh": min(entry.maximum, entry.scale_a * raw + entry.scale_b + 5),
            "acknowledge_risk": True,
        }
        result = stages.build("stage1", image, {"stage": "stage1", "intents": [intent]})
        note = stages.build_note(result, "stock_430056.bin")
        self.assertIn("версия калибровки образа: 430056", note)
        self.assertIn("покрытые версии калибровки", note)
        self.assertIn("доступные пакеты на этой версии", note)
        self.assertIn("фичи патчлиста", note)  # пояснение, почему их нет
        manifest = result.manifest
        self.assertEqual("430056", manifest["cal_version"])
        self.assertEqual(list(pol.covered_software()), manifest["covered_software"])
        self.assertNotIn("features", manifest["available_packages"])


if __name__ == "__main__":
    unittest.main()

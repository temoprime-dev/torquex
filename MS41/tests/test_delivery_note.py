import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from MS41.tools import calibration as calibration_module
from MS41.tools import generate_patches
from MS41.tools.calibration import DEFAULT_POLICY, Policy
from MS41.tools.delivery_note import build_note

ROOT = Path(__file__).parents[2]
FIRMWARE = ROOT / "MS41" / "firmware"
PARTIAL_B28 = FIRMWARE / "MS41_1429861_E36_328i_M52B28_1997_4101_partial.bin"


def generation_report(patches, outcomes=None, status="READY", notes=None, blocked=None):
    return {
        "generation": {
            "status": status,
            "source_sha256": "a" * 64,
            "image_type": "calibration-24K",
            "patches": patches,
            "outcomes": outcomes or [],
            "notes": notes or [],
            "blocked_reasons": blocked or [],
            "dry_run": {"plan_status": status, "planned_symbols": [], "findings": []},
        },
        "patch_file": {"customer": "test-client", "status": status},
    }


def patch_entry(symbol, old, new, reason="клиент просит", evidence="customer_intent",
                hint="road test", intent="vmax"):
    return {
        "symbol": symbol,
        "value": new,
        "reason": reason,
        "evidence": evidence,
        "intent": intent,
        "requires_validation": True,
        "validation_hint": hint,
        "planned": {
            "old_raw": int(old), "new_raw": int(new),
            "old_engineering": float(old), "new_engineering": float(new),
            "quantization_error_percent": 0.0, "delta_percent": None,
            "address": "0x01CA", "units": "rpm",
        },
    }


def patch_report(**overrides):
    data = {
        "status": "WRITTEN",
        "source": "/tmp/stock.bin",
        "output": "/tmp/patched.bin",
        "source_sha256": "b" * 64,
        "output_sha256": "c" * 64,
        "value_bytes_changed": 3,
        "crc_bytes_changed": 2,
        "total_bytes_changed": 5,
        "crc_blocks_recomputed": 15,
        "crc_covered_end": "0x2D4A",
        "crc_valid_after": True,
        "unplanned_byte_changes": [],
        "findings": [],
        "changes": [{
            "symbol": "c_n_max", "index": 0, "address": "0x01CA",
            "byte_offsets": ["0x01CA"], "old_raw": 203, "new_raw": 209,
            "old_engineering": 6496.0, "new_engineering": 6688.0,
            "requested_engineering": 6700.0, "quantization_error_percent": 0.18,
            "delta_percent": 2.96, "units": "rpm", "kind": "constant",
        }],
    }
    data.update(overrides)
    return data


class NoteContentTests(unittest.TestCase):
    def test_note_names_the_customer_and_the_files(self):
        note = build_note(generation_report([patch_entry("c_n_max", 6496, 6688)]),
                          patch_report())
        self.assertIn("# Наряд на калибровку MS41 — test-client", note)
        self.assertIn("/tmp/patched.bin", note)
        self.assertIn("b" * 64, note)
        self.assertIn("c" * 64, note)

    def test_note_reports_bytes_crc_and_that_the_ecu_was_not_touched(self):
        note = build_note(generation_report([patch_entry("c_n_max", 6496, 6688)]),
                          patch_report())
        self.assertIn("байтов значений: 3", note)
        self.assertIn("всего: 5", note)
        self.assertIn("блоков CRC: 15", note)
        self.assertIn("0x2D4A", note)
        self.assertIn("в ЭБУ не записан", note)

    def test_change_table_shows_values_and_computed_delta(self):
        note = build_note(generation_report([patch_entry("c_n_max", 6496, 6688)]),
                          patch_report())
        self.assertIn("| c_n_max | 0x01CA | 6496 rpm | 6688 rpm | 203 -> 209 | +2.96 % |",
                      note)

    def test_delta_from_zero_stock_is_marked_not_applicable(self):
        report = patch_report(changes=[{
            "symbol": "c_n_max_hys", "index": 0, "address": "0x01D1",
            "byte_offsets": ["0x01D1"], "old_raw": 0, "new_raw": 3,
            "old_engineering": 0.0, "new_engineering": 96.0,
            "requested_engineering": 96.0, "quantization_error_percent": 0.0,
            "delta_percent": None, "units": "rpm", "kind": "constant",
        }])
        note = build_note(generation_report([]), report)
        self.assertIn("н/д", note)

    def test_validation_is_declared_outstanding(self):
        note = build_note(generation_report([patch_entry("c_n_max", 6496, 6688)]),
                          patch_report())
        self.assertIn("НЕ ВЫПОЛНЕНА", note)
        self.assertIn("road test", note)

    def test_plan_without_a_built_binary_says_so(self):
        note = build_note(generation_report([patch_entry("c_n_max", 6496, 6688)]))
        self.assertIn("BIN ещё не собран", note)

    def test_default_reason_is_replaced_by_a_russian_label(self):
        patch = patch_entry("c_n_max_hys", 32, 96,
                            reason="overrun fuel-cut hysteresis: requested by the customer",
                            intent="hardcut")
        note = build_note(generation_report([patch]), patch_report())
        self.assertIn("обоснование клиентом не указано", note)
        self.assertIn("гистерезис отсечки", note)

    def test_customer_reason_is_kept_verbatim(self):
        patch = patch_entry("c_vs_max", 255, 250, reason="клиент просит 250 км/ч")
        note = build_note(generation_report([patch]), patch_report())
        self.assertIn("клиент просит 250 км/ч", note)


class NotFulfilledTests(unittest.TestCase):
    def test_blocked_request_is_flagged_and_reasons_are_listed(self):
        outcomes = [
            {"id": "v", "kind": "vmax", "status": "READY", "detail": "", "changes": [],
             "findings": []},
            {"id": "p", "kind": "popcorn", "status": "BLOCKED",
             "detail": "POPCORN_WITH_CATALYST: popcorn is refused on a car that still "
                       "has a catalyst", "changes": [], "findings": []},
            {"id": "r", "kind": "rpm_limit", "status": "REQUIRES_DATA",
             "detail": "raising the limiter needs acknowledge_risk", "changes": [],
             "findings": []},
        ]
        report = generation_report(
            [patch_entry("c_vs_max", 255, 250)], outcomes, status="BLOCKED",
            blocked=["p: POPCORN_WITH_CATALYST: no popcorn on a catalyst car"],
        )
        note = build_note(report, patch_report())
        self.assertIn("Наряд не полон", note)
        self.assertIn("POPCORN_WITH_CATALYST", note)
        self.assertIn("REQUIRES_DATA", note)
        self.assertIn("no popcorn on a catalyst car", note)
        self.assertNotIn("Все пункты запроса выполнены", note)

    def test_fully_fulfilled_request_says_so(self):
        outcomes = [{"id": "v", "kind": "vmax", "status": "READY", "detail": "",
                     "changes": [], "findings": []}]
        note = build_note(generation_report([patch_entry("c_vs_max", 255, 250)], outcomes),
                          patch_report())
        self.assertIn("Все пункты запроса выполнены", note)

    def test_required_logging_becomes_its_own_section(self):
        notes = ["hardware profile status: ACCEPTED_WITH_LOGGING",
                 "required logging: WOT pull in 3rd gear; idle trim check"]
        note = build_note(generation_report([], notes=notes))
        self.assertIn("## Требования к логированию", note)
        self.assertIn("WOT pull in 3rd gear", note)
        self.assertIn("idle trim check", note)

    def test_empty_plan_says_there_is_nothing_to_do(self):
        note = build_note(generation_report([], status="EMPTY"))
        self.assertIn("Записывать нечего", note)
        self.assertIn("_Изменений нет._", note)


class LongTableTests(unittest.TestCase):
    def curve_changes(self, count=154):
        return [{
            "symbol": "id_maf_tab__v_maf_1__v_maf_2 (1x256)", "index": index,
            "address": f"0x{0x2B4A + 2 * index:04X}", "byte_offsets": [],
            "old_raw": 1000 + index, "new_raw": 1100 + index,
            "old_engineering": 1000.0 + index * 2, "new_engineering": 1100.0 + index * 3,
            "requested_engineering": 1100.0 + index * 3,
            "quantization_error_percent": 0.0, "delta_percent": None,
            "units": "kg/h", "kind": "table",
        } for index in range(count)]

    def test_a_whole_curve_is_summarised_not_listed(self):
        patches = [{
            "symbol": "id_maf_tab__v_maf_1__v_maf_2 (1x256)", "value": 1100.0,
            "reason": "Sensor X transfer curve (datasheet): datasheet.pdf",
            "evidence": "documented_curve:datasheet", "intent": "maf",
            "requires_validation": True, "validation_hint": "log MAF voltage",
            "planned": {"old_raw": 1000, "new_raw": 1100, "old_engineering": 1000.0,
                        "new_engineering": 1100.0, "index": 0, "address": "0x2B4A",
                        "units": "kg/h"},
        }]
        note = build_note(generation_report(patches),
                          patch_report(changes=self.curve_changes()))
        self.assertIn("(сводно)", note)
        self.assertIn("154 столбцов", note)
        self.assertIn("сводка", note)
        self.assertNotIn("0x2B4B", note)
        self.assertIn("обоснование одинаково для всех столбцов", note)

    def test_the_note_prints_on_a_windows_console(self):
        note = build_note(generation_report(
            [patch_entry("c_n_max", 6496, 6688), patch_entry("c_vs_max", 255, 250)]),
            patch_report())
        try:
            note.encode("cp1251")   # the ANSI code page Windows uses for pipes
        except UnicodeEncodeError as exc:
            self.fail(f"the note is not cp1251-encodable on a Windows pipe ({exc})")
        self.assertIn("Дельта от стока", note)
        self.assertIn("203 -> 209", note)


    def test_short_change_lists_are_still_listed_in_full(self):
        changes = self.curve_changes(3)
        note = build_note(generation_report([]), patch_report(changes=changes))
        self.assertIn("0x2B4A", note)
        self.assertNotIn("(сводно)", note)


class CliTests(unittest.TestCase):
    def test_cli_writes_the_note_to_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            generation = Path(directory) / "generation.json"
            patch = Path(directory) / "patch.json"
            out = Path(directory) / "note.md"
            generation.write_text(json.dumps(
                generation_report([patch_entry("c_n_max", 6496, 6688)])), encoding="utf-8")
            patch.write_text(json.dumps(patch_report()), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(ROOT / "MS41" / "tools" / "delivery_note.py"),
                 "--generation", str(generation), "--patch", str(patch), "--out", str(out)],
                capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            note = out.read_text(encoding="utf-8")
        self.assertIn("Наряд на калибровку MS41", note)
        self.assertIn("c_n_max", note)

    def test_note_from_the_real_pipeline(self):
        """Run both tools on a fixture and render the note the customer would get."""
        request = {
            "version": 1, "customer": "pipeline-test", "stage": 1, "ron": 95,
            "intents": [{"id": "v", "kind": "vmax", "mode": "raise", "value_kmh": 250}],
        }
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            request_path = directory / "request.json"
            patches_path = directory / "patches.json"
            generation_path = directory / "generation.json"
            output_path = directory / "patched.bin"
            patch_report_path = directory / "patch_report.json"
            note_path = directory / "note.md"
            request_path.write_text(json.dumps(request, ensure_ascii=False),
                                    encoding="utf-8")

            result = generate_patches.generate(PARTIAL_B28, request)
            self.assertEqual(result.status, "READY")
            generation_path.write_text(json.dumps(
                {"generation": result.to_json(),
                 "patch_file": generate_patches.patches_payload(result, request, None)},
                ensure_ascii=False), encoding="utf-8")

            args = argparse.Namespace(
                bin=PARTIAL_B28, patches=directory / "patches.json", out=output_path,
                report=patch_report_path, policy=DEFAULT_POLICY,
                xdf=calibration_module.DEFAULT_XDF, json=False,
            )
            generation_path_payload = json.loads(
                generation_path.read_text(encoding="utf-8"))
            args.patches.write_text(json.dumps(
                generation_path_payload["patch_file"], ensure_ascii=False),
                encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(calibration_module.run(args), 0)
            self.assertTrue(output_path.exists())

            note = build_note(json.loads(generation_path.read_text(encoding="utf-8")),
                              json.loads(patch_report_path.read_text(encoding="utf-8")))
            note_path.write_text(note, encoding="utf-8")
            note = note_path.read_text(encoding="utf-8")

        self.assertIn("pipeline-test", note)
        self.assertIn("| c_vs_max | 0x0272 | 255 km/h | 250 km/h | 255 -> 250 | -1.96 % |", note)
        self.assertIn("Все пункты запроса выполнены", note)
        self.assertIn("НЕ ВЫПОЛНЕНА", note)

    def test_policy_is_still_the_shipped_one(self):
        self.assertTrue(Policy.load(DEFAULT_POLICY).entries)


if __name__ == "__main__":
    unittest.main()

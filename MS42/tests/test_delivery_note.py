import json
import tempfile
import unittest
from pathlib import Path

from MS42.tools import calibration as calibration_module
from MS42.tools import generate_patches as generator_module
from MS42.tools.calibration import DEFAULT_POLICY
from MS42.tools.delivery_note import (
    MAX_ROWS_PER_SYMBOL,
    VALIDATION_PENDING,
    build_note,
)

ROOT = Path(__file__).parents[2]
FIRMWARE = ROOT / "MS42" / "firmware"
B20 = FIRMWARE / "Siemens_MS42_0110C6_E46_M52TUB20_EU3.bin"


def synthetic_generation() -> dict:
    return {
        "status": "BLOCKED",
        "intents": [
            {
                "id": "v1", "kind": "vmax", "status": "READY",
                "summary": "Soft vehicle speed limit 216 -> 240 km/h",
                "findings": [], "requirements": {},
                "changes": [
                    {"symbol": "c_vs_max_1", "index": 0, "value": 240.0,
                     "evidence": "customer_intent", "intent_kind": "vmax",
                     "requires_validation": True,
                     "validation_hint": "log vehicle speed"},
                ],
            },
            {
                "id": "p1", "kind": "popcorn", "status": "REQUIRES_DATA",
                "summary": "Exhaust pops need road validation; a catalyst makes them unsafe.",
                "findings": [{"code": "EVIDENCE_REQUIRED", "severity": "reject",
                              "message": "Exhaust pops need road validation; a catalyst "
                                         "makes them unsafe."}],
                "requirements": {"summary": "Exhaust pops need road validation; a catalyst "
                                             "makes them unsafe."},
                "changes": [],
            },
        ],
    }


def synthetic_patch() -> dict:
    changes = [
        {"symbol": "c_vs_max_1", "index": 0, "address": "0x042F",
         "file_address": "0x4842F", "old_raw": 216, "new_raw": 240,
         "old_engineering": 216.0, "new_engineering": 240.0,
         "quantization_error_percent": 0.0, "units": "[km/h]"},
    ]
    for index in range(MAX_ROWS_PER_SYMBOL + 2):
        changes.append({
            "symbol": "id_n_max_mt__nvs", "index": index,
            "address": f"0x{0x769E + index:04X}", "file_address": f"0x{0x4F69E + index:05X}",
            "old_raw": 200 + index, "new_raw": 210 + index,
            "old_engineering": 6400.0 + 32 * index, "new_engineering": 6720.0 + 32 * index,
            "quantization_error_percent": 0.4, "units": "[min-1]",
        })
    return {
        "status": "WRITTEN", "source": "stock.bin", "output": "patched.bin",
        "image_type": "full-flash-512K", "value_bytes_changed": 42,
        "crc_bytes_changed": 2, "total_bytes_changed": 44,
        "crc_records_recomputed": 1, "crc_valid_after": True,
        "unplanned_byte_changes": [],
        "source_sha256": "a" * 64, "output_sha256": "b" * 64,
        "changes": changes,
    }


class NoteTests(unittest.TestCase):
    def setUp(self):
        self.note = build_note(synthetic_generation(), synthetic_patch(), "Test Customer")

    def test_title_and_sections(self):
        self.assertIn("# Наряд на калибровку MS42 — Test Customer", self.note)
        for section in ("## Что сделано", "## Изменения", "### Почему именно эти значения",
                        "## Что не выполнено", "## Что нужно проверить после прошивки",
                        "## Границы"):
            self.assertIn(section, self.note)

    def test_run_facts_come_from_the_patch_report(self):
        self.assertIn("`patched.bin`", self.note)
        self.assertIn("**WRITTEN**", self.note)
        self.assertIn("Изменено байтов значений: 42", self.note)
        self.assertIn("Пересчитано записей CRC: 1", self.note)
        self.assertIn("валидны после записи: True", self.note)
        self.assertIn("`" + "a" * 64 + "`", self.note)
        self.assertIn("Байт вне плана изменено: 0", self.note)

    def test_ms42_crc_scope_is_stated(self):
        self.assertIn("0x48000..0x4F843", self.note)
        self.assertIn("`boot` и `prog` не затрагиваются", self.note)

    def test_missing_intent_is_listed_with_its_reason(self):
        self.assertIn("| p1 |", self.note)
        self.assertIn("REQUIRES_DATA", self.note)
        self.assertIn("хлопки", self.note.lower())

    def test_validation_is_stated_as_outstanding(self):
        self.assertIn(f"**{VALIDATION_PENDING}**", self.note)
        self.assertIn("log vehicle speed", self.note)

    def test_large_tables_are_summarised_not_dumped(self):
        self.assertIn("id_n_max_mt__nvs (сводно)", self.note)
        self.assertIn("столбцов", self.note)
        self.assertNotIn("id_n_max_mt__nvs[9]", self.note)

    def test_whole_table_evidence_is_capped_and_points_at_the_manifest(self):
        # A popcorn profile or an injector scaling writes dozens of values; the
        # note lists a few per object and says where the rest lives.
        generation = {
            "status": "READY",
            "intents": [{
                "id": "p", "kind": "popcorn", "status": "READY", "summary": "", "findings": [],
                "requirements": {},
                "changes": [
                    {"symbol": "ip_t_puc_deacc__n__maf_mmv", "index": index,
                     "value": 0.5 + index / 1000, "evidence": "customer_intent",
                     "intent_kind": "popcorn", "requires_validation": True,
                     "validation_hint": "watch the temperatures"}
                    for index in range(MAX_ROWS_PER_SYMBOL + 6)
                ],
            }],
        }
        note = build_note(generation)
        self.assertIn("| ip_t_puc_deacc__n__maf_mmv |", note)
        self.assertIn("Для таблиц, записанных целиком", note)
        self.assertIn("ip_t_puc_deacc__n__maf_mmv",
                      note.split("Для таблиц, записанных целиком")[1])
        self.assertNotIn(f"| {MAX_ROWS_PER_SYMBOL + 5} |", note)

    def test_evidence_values_are_deduplicated(self):
        self.assertEqual(1, self.note.count("| c_vs_max_1 | 240 |"))

    def test_note_never_claims_validation_happened(self):
        self.assertNotIn("проверка выполнена", self.note.lower())

    def test_empty_generation_says_there_is_nothing_to_write(self):
        generation = {"status": "EMPTY", "intents": []}
        note = build_note(generation)
        self.assertIn("Записывать нечего", note)
        self.assertIn("_Изменений нет._", note)
        self.assertIn("_Все пункты запроса выполнены._", note)

    def test_blocked_generation_is_flagged(self):
        self.assertIn("**Наряд не полон.**", self.note)


class EndToEndNoteTests(unittest.TestCase):
    def test_real_pipeline_produces_a_note_that_matches_the_reports(self):
        request = {
            "intents": [
                {"id": "v", "kind": "vmax", "value_kmh": 240, "hard_add_kmh": 5.0},
                {"id": "p", "kind": "popcorn"},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            request_path = directory / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            patches = directory / "patches.json"
            generation_report = directory / "generation.json"
            code = generator_module.run(generator_module.argparse.Namespace(
                bin=B20, request=request_path, out=patches,
                report=generation_report, policy=DEFAULT_POLICY, json=False,
            ))
            self.assertEqual(0, code)
            out = directory / "out.bin"
            patch_report = directory / "patch.json"
            calibration_module.run(calibration_module.argparse.Namespace(
                bin=B20, patches=patches, out=out, report=patch_report,
                policy=DEFAULT_POLICY, xdf=calibration_module.DEFAULT_XDF,
                json=False, dry_run=False,
            ))
            note = build_note(
                json.loads(generation_report.read_text(encoding="utf-8")),
                json.loads(patch_report.read_text(encoding="utf-8")),
                "E2E",
            )
            generation = json.loads(generation_report.read_text(encoding="utf-8"))
            patch = json.loads(patch_report.read_text(encoding="utf-8"))
            self.assertIn(patch["output_sha256"], note)
            self.assertTrue(patch["crc_valid_after"])
            self.assertIn("c_vs_max_1", note)
            # The request asked for the hard-stage offset too; generation planned
            # both, and the core reported the second as already stock.
            self.assertEqual(2, len(generation["patches"]))
            self.assertEqual(1, len(patch["changes"]))
            self.assertIn("REQUIRES_DATA", note)   # popcorn stays outstanding
            self.assertIn("216", note)             # the B20 stock value


if __name__ == "__main__":
    unittest.main()

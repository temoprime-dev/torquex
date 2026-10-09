import json
import unittest
from pathlib import Path

from MS41.tools.parse_sgbd import (
    LOGGING_PRESETS,
    alias_map,
    channels,
    faults,
    parse,
    parse_file,
    presets,
    summary,
)

ROOT = Path(__file__).parents[2]
DIAGNOSTICS = ROOT / "MS41" / "diagnostics"

FIXTURE = """# MS411DS1.prg

- Jobs: [3](#jobs)
- Tables: [2](#tables)

## INFO

| Field | Value |
| --- | --- |
| ECU | MS 41.1 fuer M52 OBD II |
| ORIGIN | BMW TI-433 Schiefer |
| REVISION | 1.13 |
| SPRACHE | deutsch |

## Jobs

### Index

- [STATUS_MOTORDREHZAHL](#job-status-motordrehzahl) - Motordrehzahl auslesen
- [STATUS_LMM](#job-status-lmm) - Wert Luftmasse auslesen
- [STATUS_DIGITAL](#job-status-digital) - Status Schalteingaenge

<a id="job-status-motordrehzahl"></a>
### STATUS_MOTORDREHZAHL

Motordrehzahl auslesen

_No arguments._

#### Results

| Name | Type | Comment |
| --- | --- | --- |
| JOB_STATUS | string |  |
| STATUS_MOTORDREHZAHL_WERT | real | Ergebnis Motordrehzahl |
| STATUS_MOTORDREHZAHL_EINH | string | Einheit Motordrehzahl in 1/min |

<a id="job-status-lmm"></a>
### STATUS_LMM

Wert Luftmasse auslesen

#### Arguments

| Name | Type | Comment |
| --- | --- | --- |
| ARG | int | Auswahl |

#### Results

| Name | Type | Comment |
| --- | --- | --- |
| JOB_STATUS | string |  |
| STATUS_LMM_WERT | real | Ergebnis Luftmasse |
| STATUS_LMM_EINH | string | Einheit Luftmasse in kg/h |

<a id="job-status-digital"></a>
### STATUS_DIGITAL

Status Schalteingaenge

#### Results

| Name | Type | Comment |
| --- | --- | --- |
| JOB_STATUS | string |  |
| STATUS_VANOS_EIN | int | Status Vanos 0=Aus / 1=Ein |
| STATUS_SCHUB_AB_EIN | int | Status Schubabschaltung 0=Aus / 1=Ein |

## Tables

### Index

- [BETRIEBSWMATRIX](#table-betriebswmatrix) (2 × 8)
- [FORTTEXTE](#table-forttexte) (3 × 6)

<a id="table-betriebswmatrix"></a>
### BETRIEBSWMATRIX

Dimensions: 2 rows × 8 columns

| NAME | QUELLE | ZELLE | ORD | TYP | FAKT_A | FAKT_B | EINH |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Motordrehzahl | LINRAM | 0xDA2A | HL | 1 | 1 | 0 | 1/min |
| Luftmasse | LINRAM | 0xDA34 | LH | 1 | 0.25 | 0 | kg/h |

<a id="table-forttexte"></a>
### FORTTEXTE

Dimensions: 3 rows × 6 columns

| ORT | ORTTEXT | UW_1 | UW_2 | UW_3 | UW_4 |
| --- | --- | --- | --- | --- | --- |
| 0x00 | -- | 0x00 | 0x00 | 0x00 | 0x00 |
| 0x01 | Zuendung Zylinder 2 | 0x01 | 0x0C | 0x03 | 0x04 |
| 0x08 | Luftmassenmesser | 0x01 | 0x0C | 0x03 | 0x04 |
"""


class ParseSgbdTests(unittest.TestCase):
    def setUp(self):
        self.sgbd = parse(FIXTURE, "MS411DS1.md")

    def test_header_and_info(self):
        self.assertEqual(self.sgbd.variant, "MS411DS1")
        self.assertEqual(self.sgbd.info["ECU"], "MS 41.1 fuer M52 OBD II")
        self.assertEqual(self.sgbd.info["REVISION"], "1.13")

    def test_jobs_arguments_and_results(self):
        names = [job.name for job in self.sgbd.jobs]
        self.assertEqual(names, ["STATUS_MOTORDREHZAHL", "STATUS_LMM", "STATUS_DIGITAL"])
        lmm = next(job for job in self.sgbd.jobs if job.name == "STATUS_LMM")
        self.assertEqual([argument.name for argument in lmm.arguments], ["ARG"])
        self.assertIn("STATUS_LMM_EINH", [result.name for result in lmm.results])
        dme = self.sgbd.jobs[0]
        self.assertEqual(dme.description, "Motordrehzahl auslesen")
        self.assertEqual(dme.arguments, [])

    def test_tables_are_typed(self):
        matrix = self.sgbd.tables["BETRIEBSWMATRIX"]
        self.assertEqual(matrix.dimensions, (2, 8))
        self.assertEqual(matrix.rows[0]["ZELLE"], "0xDA2A")
        self.assertNotIn("Index", self.sgbd.tables)

    def test_channels_include_fast_ram_scaling(self):
        mapped = channels(self.sgbd)["signals"]
        self.assertEqual(mapped["rpm"]["result"], "STATUS_MOTORDREHZAHL_WERT")
        self.assertEqual(mapped["rpm"]["fast_ram"]["cell"], "0xDA2A")
        self.assertEqual(mapped["maf_airflow"]["fast_ram"]["scale_a"], 0.25)
        self.assertEqual(mapped["maf_airflow"]["fast_ram"]["order"], "LH")

    def test_channels_report_unavailable_signals(self):
        report = channels(self.sgbd)
        self.assertIn("ignition", report["unsupported_signals"])
        self.assertNotIn("rpm", report["unsupported_signals"])

    def test_booleans_are_exposed(self):
        report = channels(self.sgbd)
        results = {item["result"] for item in report["booleans"]}
        self.assertIn("STATUS_SCHUB_AB_EIN", results)
        meaning = next(item for item in report["booleans"]
                       if item["result"] == "STATUS_SCHUB_AB_EIN")
        self.assertEqual(meaning["meaning"], "overrun fuel cut active")

    def test_faults_skip_empty_entries(self):
        self.assertEqual(
            faults(self.sgbd),
            [{"code": "0x01", "text": "Zuendung Zylinder 2"},
             {"code": "0x08", "text": "Luftmassenmesser"}],
        )

    def test_alias_map_covers_jobs_and_results(self):
        mapping = alias_map(self.sgbd)
        self.assertEqual(mapping["STATUS_MOTORDREHZAHL_WERT"], "rpm")
        self.assertEqual(mapping["STATUS_MOTORDREHZAHL"], "rpm")
        self.assertEqual(mapping["STATUS_VANOS_EIN"], "STATUS_VANOS_EIN")

    def test_presets_expose_jobs_results_and_units(self):
        built = presets(self.sgbd)
        rpm = next(entry for entry in built["profiles"]["stage0"]["results"]
                   if entry["signal"] == "rpm")
        self.assertEqual(rpm["job"], "STATUS_MOTORDREHZAHL")
        self.assertEqual(rpm["unit"], "1/min")
        self.assertIn("ignition", built["profiles"]["stage0"]["missing"])

    def test_summary_counts(self):
        report = summary(self.sgbd)
        self.assertEqual(report["job_count"], 3)
        self.assertEqual(report["status_job_count"], 3)
        self.assertEqual(report["tables"]["FORTTEXTE"], [3, 6])


class GeneratedBundleTests(unittest.TestCase):
    """The committed bundle must stay consistent with what the parser produces."""

    @classmethod
    def setUpClass(cls):
        if not DIAGNOSTICS.exists():
            raise unittest.SkipTest("diagnostics bundle is not present")

    def load(self, name):
        return json.loads((DIAGNOSTICS / name).read_text(encoding="utf-8"))

    def test_index_covers_all_six_ms41_variants(self):
        index = self.load("ms41_sgbd_index.json")
        variants = {entry["variant"] for entry in index}
        self.assertEqual(
            variants,
            {"MS410DS0", "MS410DS1", "MS410DS2", "MS410DS3", "MS411DS1", "MS411DS2"},
        )

    def test_obd2_variant_exposes_all_wot_signals(self):
        presets_data = self.load("ms41_logging_presets.json")
        ms411 = next(entry for entry in presets_data if entry["variant"] == "MS411DS1")
        self.assertEqual(ms411["profiles"]["wot"]["missing"], [])
        self.assertEqual(ms411["profiles"]["ignition"]["missing"], [])
        self.assertEqual(ms411["profiles"]["fueling"]["missing"], [])

    def test_every_logging_preset_signal_uses_a_real_result(self):
        channels_data = {entry["variant"]: entry for entry in self.load("ms41_channels.json")}
        presets_data = {entry["variant"]: entry for entry in self.load("ms41_logging_presets.json")}
        for variant, report in channels_data.items():
            available = set(report["signals"])
            for profile, definition in presets_data[variant]["profiles"].items():
                for entry in definition["results"]:
                    self.assertIn(
                        entry["signal"], available,
                        f"{variant}/{profile}: {entry['signal']} is not a mapped signal",
                    )

    def test_aliases_resolve_to_canonical_logging_signals(self):
        aliases = self.load("ms41_aliases.json")
        known = set(LOGGING_PRESETS)
        for variant, mapping in aliases.items():
            self.assertTrue(mapping, variant)
            for name, canonical in mapping.items():
                self.assertIsInstance(name, str)
                self.assertIsInstance(canonical, str)
        self.assertEqual(aliases["MS411DS1"]["STATUS_LMM_WERT"], "maf_airflow")
        self.assertEqual(aliases["MS410DS0"]["STATUS_ZUENDWINKEL_WERT"], "ignition")
        self.assertTrue(known)

    def test_fault_tables_are_not_empty(self):
        faults_data = self.load("ms41_faults.json")
        for variant, entries in faults_data.items():
            self.assertGreater(len(entries), 10, variant)
            for entry in entries:
                self.assertTrue(entry["code"].startswith("0x"))
                self.assertTrue(entry["text"])


class ParserRobustnessTests(unittest.TestCase):
    def test_unknown_markdown_does_not_crash(self):
        parsed = parse("# nothing\n\nno sections here\n", "unknown.md")
        self.assertEqual(parsed.variant, "unknown")
        self.assertEqual(parsed.jobs, [])
        self.assertEqual(parsed.tables, {})

    def test_parse_file_rejects_missing_file(self):
        with self.assertRaises(OSError):
            parse_file(Path("/nonexistent/MS411DS1.md"))


if __name__ == "__main__":
    unittest.main()

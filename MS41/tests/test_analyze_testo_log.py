import tempfile
import unittest
from pathlib import Path

from MS41.tools.analyze_testo_log import _time, analyze_log, canonical_name


class TimestampTests(unittest.TestCase):
    """``datetime.timestamp()`` is not usable on Windows for old dates."""

    def test_clock_only_stamps_keep_their_spacing(self):
        # strptime fills in 1900-01-01 for a bare clock, and .timestamp() raised
        # OSError on Windows for it, which killed the whole parse
        first = _time("00:00:00", False)
        second = _time("01:00:00", False)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertAlmostEqual(second - first, 3600.0, places=6)

    def test_a_full_date_before_1970_is_accepted(self):
        self.assertAlmostEqual(_time("01.01.1900 00:00:00", False),
                               -2208988800.0, places=6)

    def test_milliseconds_survive(self):
        self.assertAlmostEqual(_time("00:00:01,500", False)
                               - _time("00:00:00", False), 1.5, places=6)


class AnalyzeTestoLogTests(unittest.TestCase):
    def write_log(self, content: str, encoding: str = "utf-8"):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "testo.csv"
        path.write_bytes(content.encode(encoding))
        self.addCleanup(directory.cleanup)
        return path

    def test_native_ediabas_names_are_mapped(self):
        self.assertEqual(canonical_name("STAT_MOTORDREHZAHL_WERT [rpm]"), "rpm")
        self.assertEqual(canonical_name("STATUS_LMM_WERT (kg/h)"), "maf_airflow")
        self.assertEqual(canonical_name("Ansauglufttemperatur [°C]"), "intake_temp")

    def test_reads_v2_style_comma_csv(self):
        header = (
            "Time,RPM,Throttle [%],Engine Load [mg/stroke],MAF [kg/h],"
            "MAF Voltage [V],Ignition Timing [deg],Injector Pulse Width [ms],Wideband Lambda\n"
        )
        rows = "".join(
            f"{i / 10:.1f},{2000+i*50},{50+i},500,{200+i},3.2,20,12.5,0.86\n"
            for i in range(60)
        )
        report = analyze_log(self.write_log(header + rows), "wot")
        self.assertEqual(report.status, "READY_FOR_ANALYSIS")
        self.assertEqual(report.rows, 60)
        self.assertAlmostEqual(report.sample_rate_hz, 10.0, places=3)
        self.assertFalse(report.missing_required)

    def test_reads_legacy_semicolon_decimal_comma_cp1252(self):
        content = (
            "Zeit;STAT_MOTORDREHZAHL_WERT [rpm];STATUS_LMM_WERT [kg/h];"
            "Zündwinkel [deg];Kühlmitteltemperatur [C];Ansauglufttemperatur [C]\n"
            "12:00:00,000;800,0;20,5;8,0;90,0;25,0\n"
            "12:00:00,100;810,0;21,0;8,5;90,0;25,0\n"
        )
        report = analyze_log(self.write_log(content, "cp1252"), "stage0")
        self.assertEqual(report.status, "READY_FOR_ANALYSIS")
        self.assertEqual(report.encoding, "cp1252")
        rpm = next(channel for channel in report.channels if channel.canonical == "rpm")
        self.assertEqual(rpm.minimum, 800.0)

    def test_missing_wideband_and_ipw_is_incomplete(self):
        report = analyze_log(self.write_log("time,rpm,load\n0,1000,100\n1,2000,200\n"), "fueling")
        self.assertEqual(report.status, "INCOMPLETE")
        self.assertIn("lambda_wideband", report.missing_required)
        self.assertIn("injector_pulse_width", report.missing_required)

    def test_exact_ediabas_ms41_names_from_generated_aliases(self):
        self.assertEqual(canonical_name("STATUS_MOTORDREHZAHL_WERT"), "rpm")
        self.assertEqual(canonical_name("STATUS_LMM_WERT"), "maf_airflow")
        self.assertEqual(canonical_name("STATUS_EINSPRITZZEIT_WERT"), "injector_pulse_width")
        self.assertEqual(canonical_name("STATUS_KLOPF_ADC1_WERT"), "knock_bank1")
        self.assertEqual(canonical_name("STATUS_L_SONDE_WERT"), "lambda_bank1")

    def test_real_testo_export_is_accepted_and_knock_banks_satisfy_knock(self):
        header = (
            "Time;STATUS_MOTORDREHZAHL_WERT;STATUS_LAST_WERT;STATUS_ZUENDWINKEL_WERT;"
            "STATUS_MOTORTEMPERATUR_WERT;STATUS_AN_LUFTTEMPERATUR_WERT;"
            "STATUS_KLOPF_ADC1_WERT;STATUS_KLOPF_ADC2_WERT\n"
        )
        rows = "".join(
            f"12:00:{i // 10:02d};{2000 + i * 20};450;18.5;90;25;1.2;1.1\n"
            for i in range(40)
        )
        report = analyze_log(self.write_log(header + rows), "ignition")
        self.assertEqual(report.status, "READY_FOR_ANALYSIS")
        self.assertNotIn("knock", report.missing_required)

    def test_narrowband_only_warns_that_wot_fueling_cannot_be_judged(self):
        header = (
            "time,rpm,load,maf_airflow [kg/h],maf_voltage [V],"
            "injector_pulse_width [ms],lambda_bank1 [V],lambda_bank2 [V]\n"
        )
        rows = "".join(f"{i * 0.1:.1f},{2000 + i * 20},450,200,3.2,12.5,0.8,0.8\n"
                       for i in range(30))
        report = analyze_log(self.write_log(header + rows), "fueling")
        self.assertIn("lambda_wideband", report.missing_required)
        self.assertTrue(
            any("narrowband" in warning.lower() for warning in report.warnings),
            report.warnings,
        )

    def test_rejects_empty_log(self):
        with self.assertRaises(ValueError):
            analyze_log(self.write_log(""), "wot")


if __name__ == "__main__":
    unittest.main()

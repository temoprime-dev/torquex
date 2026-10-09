# -*- coding: utf-8 -*-
"""Ворота варианта 1 (`tools/variant1.py`): октан, правовая справка, откат, опции.

Задание `docs/TASK_VARIANT1_RU.md` §1 (состав), §2 (правовая справка),
§5.3 (генератор). Тесты не зависят от образов ЭБУ: модуль работает только с
заявкой клиента и нарядом.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import variant1  # noqa: E402


class OctaneTests(unittest.TestCase):
    """Профиль топлива: клиент выбирает, добавку получает только RON98."""

    def test_request_field_sets_profile(self):
        findings: list[dict] = []
        self.assertEqual("ron98", variant1.octane_choice({"octane": "ron98"}, [], findings))
        self.assertEqual([], findings)

    def test_aliases_and_case(self):
        for raw, expected in (("98", "ron98"), ("RON95", "ron95"), ("91", "ron91"),
                              ("Ron_98", "ron98")):
            findings: list[dict] = []
            self.assertEqual(expected,
                             variant1.octane_choice({"octane": raw}, [], findings), raw)
            self.assertEqual([], findings)

    def test_intent_can_set_profile(self):
        findings: list[dict] = []
        intents = [{"id": "o1", "kind": "octane", "fuel": "ron95"}]
        self.assertEqual("ron95", variant1.octane_choice({}, intents, findings))
        self.assertEqual([], findings)

    def test_unknown_fuel_is_rejected(self):
        findings: list[dict] = []
        self.assertIsNone(variant1.octane_choice({"octane": "100"}, [], findings))
        self.assertEqual("OCTANE_PROFILE_UNKNOWN", findings[0]["code"])
        self.assertEqual("reject", findings[0]["severity"])

    def test_missing_fuel_in_intent_is_rejected(self):
        findings: list[dict] = []
        self.assertIsNone(
            variant1.octane_choice({}, [{"id": "o1", "kind": "octane"}], findings))
        self.assertEqual("OCTANE_PROFILE_UNKNOWN", findings[0]["code"])

    def test_conflicting_declarations_are_rejected(self):
        findings: list[dict] = []
        intents = [{"id": "o1", "kind": "octane", "fuel": "ron98"}]
        self.assertIsNone(
            variant1.octane_choice({"octane": "ron95"}, intents, findings))
        self.assertEqual("OCTANE_PROFILE_CONFLICT", findings[0]["code"])

    def test_ron91_rejects_any_ignition_delta(self):
        for delta in (0.75, 1.5, 5.0):
            findings: list[dict] = []
            variant1.enforce_octane(
                "ron91", [{"id": "i1", "kind": "ignition", "delta_deg": delta}], findings)
            self.assertEqual("OCTANE_PROFILE_NO_ADDITION", findings[0]["code"], delta)
            self.assertEqual("reject", findings[0]["severity"], delta)

    def test_profile_max_right_is_the_stage_two_ceiling(self):
        """enforce_octane отсекает только явное превышение права профиля;
        «добавка без лога выше Stage 1» — работа пакета (нужен лог)."""
        findings: list[dict] = []
        variant1.enforce_octane(
            "ron95", [{"id": "i1", "kind": "ignition", "delta_deg": 1.5}], findings)
        self.assertEqual([], findings)
        findings = []
        variant1.enforce_octane(
            "ron95", [{"id": "i1", "kind": "ignition", "delta_deg": 5.01}], findings)
        self.assertEqual("OCTANE_PROFILE_LIMIT", findings[0]["code"])
        findings = []
        variant1.enforce_octane(
            "ron98", [{"id": "i1", "kind": "ignition", "delta_deg": 3.0}], findings)
        self.assertEqual([], findings)

    def test_ron98_allows_addition_and_none_is_neutral(self):
        for profile in ("ron98", None):
            findings: list[dict] = []
            variant1.enforce_octane(
                profile, [{"id": "i1", "kind": "ignition", "delta_deg": 3.0}], findings)
            self.assertEqual([], findings, profile)

    def test_zero_and_missing_delta_are_not_touched(self):
        findings: list[dict] = []
        variant1.enforce_octane(
            "ron95",
            [{"id": "i1", "kind": "ignition", "delta_deg": 0},
             {"id": "i2", "kind": "ignition"}],
            findings,
        )
        self.assertEqual([], findings)

    def test_report_shape(self):
        report = variant1.octane_report("ron98")
        self.assertEqual("ron98", report["profile"])
        self.assertEqual("RON98", report["fuel"])
        self.assertTrue(report["add_allowed"])
        self.assertIn("RON91", {item["title"] for item in report["profiles"].values()})
        empty = variant1.octane_report(None)
        self.assertIsNone(empty["profile"])
        self.assertFalse(empty["add_allowed"])
        self.assertTrue(empty["note_ru"])


class LegalityTests(unittest.TestCase):
    """Правовая справка: хлопки/диагностика — только с подтверждением клиента."""

    def test_not_relevant_means_no_findings(self):
        findings: list[dict] = []
        self.assertTrue(variant1.legality_gate(
            {}, [{"id": "i1", "kind": "ignition"}], findings, ("popcorn", "dtc")))
        self.assertEqual([], findings)

    def test_request_level_acknowledgement(self):
        findings: list[dict] = []
        self.assertTrue(variant1.legality_gate(
            {"acknowledge_legality": True},
            [{"id": "p1", "kind": "popcorn"}], findings, ("popcorn", "dtc")))
        self.assertEqual([], findings)

    def test_intent_level_acknowledgement(self):
        findings: list[dict] = []
        self.assertTrue(variant1.legality_gate(
            {}, [{"id": "p1", "kind": "dtc", "acknowledge_legality": True}],
            findings, ("popcorn", "dtc")))
        self.assertEqual([], findings)

    def test_missing_acknowledgement_is_a_reject(self):
        findings: list[dict] = []
        self.assertFalse(variant1.legality_gate(
            {}, [{"id": "p1", "kind": "popcorn"}], findings, ("popcorn", "dtc")))
        self.assertEqual("ACKNOWLEDGE_LEGALITY_REQUIRED", findings[0]["code"])
        self.assertEqual("reject", findings[0]["severity"])
        self.assertIn("p1", findings[0]["message"])

    def test_notice_payload_has_both_languages_and_references(self):
        payload = variant1.legal_notice_payload()
        self.assertEqual(variant1.LEGAL_NOTICE_DATE, payload["date"])
        ru = " ".join(payload["text_ru"])
        en = " ".join(payload["text_en"])
        for fragment in ("715/2007", "3", "5", "1068.101", "acknowledge_legality"):
            self.assertIn(fragment, ru)
        for fragment in ("715/2007", "203(a)(3)", "1068.101(b)"):
            self.assertIn(fragment, en)
        markdown = variant1.legal_notice_markdown()
        self.assertIn("## Правовая справка", markdown)
        self.assertIn("Legal notice (EN)", markdown)


class RollbackTests(unittest.TestCase):
    """Откат на сток: sha256, инструкция возврата, проверка файла."""

    SHA = "ab" * 32

    def test_block_keeps_stock_hash_and_instructions(self):
        block = variant1.rollback_block(self.SHA, image_kind="образ 512 КиБ")
        self.assertEqual(self.SHA, block["stock_sha256"])
        self.assertEqual("образ 512 КиБ", block["image_kind"])
        self.assertGreaterEqual(len(block["instructions_ru"]), 3)
        self.assertNotIn("verify_command", block)

    def test_verify_command_is_appended_to_instructions(self):
        block = variant1.rollback_block(
            self.SHA, verify_command="python3 -m MS45.tools.stages --verify-manifest m.json")
        self.assertIn("--verify-manifest", " ".join(block["instructions_ru"]))
        markdown = variant1.rollback_markdown(block)
        self.assertIn("## Откат на сток", markdown)
        self.assertIn(self.SHA, markdown)
        self.assertIn("--verify-manifest", markdown)


class OptionTests(unittest.TestCase):
    """Опции варианта 1, которые честно ждут данных."""

    def test_every_option_has_reason_and_needs(self):
        for kind in variant1.OPTION_TITLES:
            self.assertTrue(variant1.option_why(kind), kind)
            needs = variant1.option_needs(kind)
            self.assertGreaterEqual(len(needs), 3, kind)
            self.assertTrue(all(isinstance(item, str) and item for item in needs), kind)

    def test_options_requested_groups_intents(self):
        requested = variant1.options_requested(
            [{"id": "d1", "kind": "disa_off"}, {"id": "d2", "kind": "isa_off"},
             {"id": "i1", "kind": "ignition"}, {"id": "d3", "kind": "disa_off"}])
        self.assertEqual({"disa_off": ["d1", "d3"], "isa_off": ["d2"]}, requested)

    def test_unknown_kind_gives_empty_answers(self):
        self.assertEqual((), variant1.option_needs("bogus"))
        self.assertEqual("", variant1.option_why("bogus"))


if __name__ == "__main__":
    unittest.main()

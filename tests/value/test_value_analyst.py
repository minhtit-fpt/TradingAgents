"""Tier 3: the assessment must come back typed, cached once, and charged once."""

import tempfile
import unittest
from pathlib import Path

from tradingagents.value.analyst import value_analyst
from tradingagents.value.analyst.schemas import ValueAssessment, Verdict
from tradingagents.value.edgar.filings import Sections
from tradingagents.value.llm.budget import Budget

SECTIONS = Sections(
    business="We sell industrial fasteners through 3,400 branches.",
    risk_factors="A prolonged construction downturn would reduce branch demand.",
    mdna="Net sales rose 8.2% on price rather than volume.",
    dropped=(("mdna", 12_000),),
)
NUMBERS = "FAST: ALERT — MoS +31.2% (price 62.10 vs value 90.30)"

ASSESSMENT = ValueAssessment(
    ticker="FAST",
    verdict=Verdict.PROCEED,
    moat="wide",
    moat_trend="stable",
    customer_concentration="none_disclosed",
    accounting_flags=[],
    key_risks=["Construction cycle exposure"],
    thesis="Distribution density is the advantage; losing branch economics would break it.",
    confidence="high",
    evidence_gaps=[],
)


class _Raw:
    def __init__(self, input_tokens=30_000, output_tokens=600):
        self.usage_metadata = {"input_tokens": input_tokens, "output_tokens": output_tokens}


class _Structured:
    def __init__(self, owner, response):
        self._owner = owner
        self._response = response

    def invoke(self, prompt):
        self._owner.calls += 1
        self._owner.prompts.append(prompt)
        return self._response


class _FakeLLM:
    """A chat model that returns whatever ``response`` it was handed."""

    def __init__(self, response=None):
        self.calls = 0
        self.prompts = []
        self.include_raw = None
        self._response = (
            response if response is not None else {"parsed": ASSESSMENT, "raw": _Raw()}
        )

    def with_structured_output(self, schema, include_raw=False, **kwargs):
        assert schema is ValueAssessment
        self.include_raw = include_raw
        return _Structured(self, self._response)


class PromptTest(unittest.TestCase):
    def test_prompt_is_identical_for_identical_inputs(self):
        # The prompt is the cache key, so instability here means paying twice.
        first = value_analyst.build_prompt("FAST", SECTIONS, NUMBERS)
        second = value_analyst.build_prompt("FAST", SECTIONS, NUMBERS)

        self.assertEqual(first, second)
        self.assertTrue(first.startswith(value_analyst.SYSTEM_PROMPT))

    def test_prompt_carries_the_sections_and_what_was_truncated(self):
        prompt = value_analyst.build_prompt("FAST", SECTIONS, NUMBERS, company_name="Fastenal")

        self.assertIn("Fastenal", prompt)
        self.assertIn("industrial fasteners", prompt)
        self.assertIn("construction downturn", prompt)
        self.assertIn("12,000 characters dropped", prompt)
        self.assertIn(NUMBERS, prompt)

    def test_a_missing_section_is_declared_rather_than_left_blank(self):
        sections = Sections(business="", risk_factors="risks", mdna="mdna")

        prompt = value_analyst.build_prompt("FAST", sections, NUMBERS)

        self.assertIn("Sections not found: business", prompt)
        self.assertIn("(not found in the filing)", prompt)


class SchemaTest(unittest.TestCase):
    """Flatness is a DeepSeek constraint, not a style choice (plan section 8).

    A live function-calling round-trip needs an API key and costs money, so what
    is checked here is the property that makes that round-trip reliable: every
    field is a scalar, an enum of strings, or a list of strings. Nested models
    and dicts are what come back malformed.
    """

    def test_every_field_is_a_scalar_an_enum_or_a_list_of_strings(self):
        schema = ValueAssessment.model_json_schema()
        defs = schema.get("$defs", {})

        for name, spec in schema["properties"].items():
            with self.subTest(field=name):
                if "$ref" in spec or "allOf" in spec:
                    ref = spec.get("$ref") or spec["allOf"][0]["$ref"]
                    target = defs[ref.rsplit("/", 1)[-1]]
                    self.assertEqual(target.get("type"), "string", "enums must be string-valued")
                    self.assertIn("enum", target)
                    continue
                if spec.get("type") == "array":
                    self.assertEqual(spec["items"], {"type": "string"})
                    continue
                self.assertIn(spec.get("type"), {"string", "integer", "number", "boolean"})

    def test_no_definition_nests_another_model(self):
        for name, spec in ValueAssessment.model_json_schema().get("$defs", {}).items():
            with self.subTest(definition=name):
                self.assertNotIn("properties", spec, "nested models break function calling")


class AssessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.budget = Budget(ledger_path=self.tmp / "ledger.jsonl")

    def _assess(self, llm):
        return value_analyst.assess(
            "FAST", SECTIONS, NUMBERS, llm=llm,
            model="deepseek-v4-pro", budget=self.budget, cache_dir=self.tmp / "cache",
        )

    def test_returns_a_validated_assessment(self):
        llm = _FakeLLM()

        assessment = self._assess(llm)

        self.assertIsInstance(assessment, ValueAssessment)
        self.assertEqual(assessment.verdict, Verdict.PROCEED)
        self.assertEqual(assessment.key_risks, ["Construction cycle exposure"])
        # Usage metadata only rides on the raw message, so include_raw is required.
        self.assertTrue(llm.include_raw)

    def test_repeat_of_the_same_prompt_costs_nothing(self):
        llm = _FakeLLM()

        first = self._assess(llm)
        spend_after_first = self.budget.run_spend_usd
        second = self._assess(llm)

        self.assertEqual(llm.calls, 1)
        self.assertEqual(first, second)
        self.assertEqual(self.budget.run_spend_usd, spend_after_first)

    def test_the_call_is_charged_against_the_budget(self):
        self._assess(_FakeLLM())

        # 30k in at $1.32/Mtok + 600 out at $3.96/Mtok — DeepSeek's peak rates,
        # hardcoded on purpose: a silent edit to MODEL_PRICES_USD_PER_MTOK should
        # break a test rather than quietly change what every cap is worth.
        self.assertAlmostEqual(self.budget.run_spend_usd, 0.0396 + 0.002376, places=6)

    def test_an_unparsed_response_raises_instead_of_falling_back_to_prose(self):
        llm = _FakeLLM({"parsed": None, "raw": _Raw(), "parsing_error": "no tool call"})

        with self.assertRaises(value_analyst.ValueAnalystError):
            self._assess(llm)

    def test_an_unparsed_response_is_still_charged(self):
        # The tokens were spent; a loop of malformed answers must still hit the cap.
        llm = _FakeLLM({"parsed": None, "raw": _Raw(), "parsing_error": "no tool call"})

        with self.assertRaises(value_analyst.ValueAnalystError):
            self._assess(llm)

        self.assertGreater(self.budget.run_spend_usd, 0.0)

    def test_a_response_without_token_usage_raises(self):
        # An uncharged call is how a budget cap silently stops capping.
        class _NoUsage:
            usage_metadata = None

        llm = _FakeLLM({"parsed": ASSESSMENT, "raw": _NoUsage()})

        with self.assertRaises(value_analyst.ValueAnalystError):
            self._assess(llm)


if __name__ == "__main__":
    unittest.main()

"""Cache behaviour: an identical prompt must never hit the network twice."""

import tempfile
import unittest
from pathlib import Path

from tradingagents.value.llm import cache
from tradingagents.value.llm.budget import Budget, BudgetExceeded


class _Provider:
    """Stand-in for a real LLM call; counts how often it is actually invoked."""

    def __init__(self, text="ok", prompt_tokens=1000, completion_tokens=200):
        self.calls = 0
        self._result = cache.LLMResult(text, prompt_tokens, completion_tokens)

    def __call__(self):
        self.calls += 1
        return self._result


class CacheTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name) / "llm_cache"
        self.addCleanup(self._tmp.cleanup)

    def test_identical_prompt_calls_provider_once(self):
        provider = _Provider(text="cached answer")

        first = cache.cached_call("deepseek", "deepseek-v4-flash", "prompt", provider,
                                  cache_dir=self.cache_dir)
        second = cache.cached_call("deepseek", "deepseek-v4-flash", "prompt", provider,
                                   cache_dir=self.cache_dir)

        self.assertEqual(provider.calls, 1)
        self.assertEqual(first, second)
        self.assertEqual(second.text, "cached answer")

    def test_cache_survives_a_new_process(self):
        # The entry is on disk, not in memory, so a re-run of the daily job is free.
        cache.cached_call("deepseek", "deepseek-v4-flash", "p", _Provider(),
                          cache_dir=self.cache_dir)
        provider = _Provider()

        cache.cached_call("deepseek", "deepseek-v4-flash", "p", provider,
                          cache_dir=self.cache_dir)

        self.assertEqual(provider.calls, 0)

    def test_different_model_or_prompt_is_a_miss(self):
        provider = _Provider()

        cache.cached_call("deepseek", "deepseek-v4-flash", "p", provider,
                          cache_dir=self.cache_dir)
        cache.cached_call("deepseek", "deepseek-v4-pro", "p", provider,
                          cache_dir=self.cache_dir)
        cache.cached_call("deepseek", "deepseek-v4-flash", "other", provider,
                          cache_dir=self.cache_dir)

        self.assertEqual(provider.calls, 3)

    def test_hit_is_not_charged_against_the_budget(self):
        budget = Budget(ledger_path=Path(self._tmp.name) / "ledger.jsonl",
                        run_cap_usd=1.0, month_cap_usd=1.0)
        provider = _Provider()

        cache.cached_call("deepseek", "deepseek-v4-flash", "p", provider,
                          cache_dir=self.cache_dir, budget=budget)
        after_miss = budget.run_spend_usd
        cache.cached_call("deepseek", "deepseek-v4-flash", "p", provider,
                          cache_dir=self.cache_dir, budget=budget)

        self.assertGreater(after_miss, 0.0)
        self.assertEqual(budget.run_spend_usd, after_miss)

    def test_response_is_cached_even_when_the_call_trips_the_cap(self):
        # The money is already spent; losing the response would mean paying twice.
        budget = Budget(ledger_path=Path(self._tmp.name) / "ledger.jsonl",
                        run_cap_usd=0.0, month_cap_usd=100.0)
        provider = _Provider()

        with self.assertRaises(BudgetExceeded):
            cache.cached_call("deepseek", "deepseek-v4-flash", "p", provider,
                              cache_dir=self.cache_dir, budget=budget)

        cached = cache.load(cache.cache_key("deepseek", "deepseek-v4-flash", "p"),
                            cache_dir=self.cache_dir)
        self.assertIsNotNone(cached)
        self.assertEqual(provider.calls, 1)

    def test_corrupt_entry_raises_instead_of_returning_garbage(self):
        key = cache.cache_key("deepseek", "deepseek-v4-flash", "p")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / f"{key}.json").write_text("{not json", encoding="utf-8")

        with self.assertRaises(ValueError):
            cache.load(key, cache_dir=self.cache_dir)


if __name__ == "__main__":
    unittest.main()

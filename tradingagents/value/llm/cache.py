"""On-disk LLM response cache keyed by (provider, model, prompt).

Purpose is twofold: a re-run of a job costs nothing, and the tier-3 backtest can
be replayed without paying for it twice. The key is a SHA-256 of the exact
prompt, so any change to the prompt is a miss — that is intended, a stale answer
to a changed question is worse than a paid one.
"""

import hashlib
import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from ..config import LLM_CACHE_DIR
from .budget import Budget


class LLMResult(NamedTuple):
    """What a provider call must return for it to be cacheable and chargeable."""

    text: str
    prompt_tokens: int
    completion_tokens: int


def cache_key(provider: str, model: str, prompt: str) -> str:
    """Stable key over the three things that change the answer."""
    digest = hashlib.sha256()
    # NUL separator: cannot appear in the parts, so no ambiguity between them.
    digest.update("\x00".join((provider, model, prompt)).encode("utf-8"))
    return digest.hexdigest()


def load(key: str, cache_dir: Path | None = None) -> LLMResult | None:
    """Return the cached result for ``key``, or ``None`` on a miss."""
    path = _entry_path(key, cache_dir)
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        return LLMResult(
            text=entry["text"],
            prompt_tokens=int(entry["prompt_tokens"]),
            completion_tokens=int(entry["completion_tokens"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        # A corrupt entry is a bug worth seeing, not a silent re-spend.
        raise ValueError(f"Corrupt LLM cache entry {path}: {exc}") from exc


def store(
    key: str,
    provider: str,
    model: str,
    prompt: str,
    result: LLMResult,
    cache_dir: Path | None = None,
) -> Path:
    """Write one cache entry atomically and return its path."""
    path = _entry_path(key, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "provider": provider,
        "model": model,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "text": result.text,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entry), encoding="utf-8")
    os.replace(tmp, path)
    return path


def cached_call(
    provider: str,
    model: str,
    prompt: str,
    fn: Callable[[], LLMResult],
    cache_dir: Path | None = None,
    budget: Budget | None = None,
) -> LLMResult:
    """Return a cached response, or call ``fn`` once and cache what it returns.

    A hit performs no network call and is not charged — the money was spent when
    the entry was first written. On a miss the response is cached *before* the
    budget is charged, so a call that trips the cap is not paid for twice.
    """
    key = cache_key(provider, model, prompt)
    hit = load(key, cache_dir)
    if hit is not None:
        return hit

    result = fn()
    store(key, provider, model, prompt, result, cache_dir)
    if budget is not None:
        budget.charge(model, result.prompt_tokens, result.completion_tokens)
    return result


def _entry_path(key: str, cache_dir: Path | None) -> Path:
    return (Path(cache_dir) if cache_dir else LLM_CACHE_DIR) / f"{key}.json"

"""One-shot ingest: pull 10-K facts into the store and report tag coverage.

    python -m tradingagents.value.jobs.bootstrap --limit 100
    python -m tradingagents.value.jobs.bootstrap --tickers AAPL,MSFT,KO

Run by hand, never from cron. Requires ``VALUE_SEC_USER_AGENT`` to carry a real
contact address — SEC blocks anonymous traffic, and EDGAR is the only source of
statements this module has.
"""

import argparse
import sys
from datetime import date

from ..config import HISTORY_YEARS, MIN_CONCEPT_COVERAGE
from ..edgar.client import SecClient, SecRequestError
from ..edgar.companyfacts import companies, fetch_companyfacts
from ..edgar.concepts import CONCEPT_NAMES, annual_facts
from ..store import db


def ingest(conn, client: SecClient, ticker: str, cik: int) -> int:
    """Fetch one company's facts and write them. Returns the row count."""
    payload = fetch_companyfacts(client, cik)
    return db.upsert_facts(conn, ticker, cik, annual_facts(payload))


def coverage_lines(conn, ticker: str, as_of: str, years: int) -> list[str]:
    """The per-concept coverage report — a deliverable, not a debug print."""
    report = db.coverage(conn, ticker, as_of, years)
    ratio = db.coverage_ratio(report, years)
    verdict = "screenable" if ratio >= MIN_CONCEPT_COVERAGE else "EXCLUDED (low coverage)"
    lines = [f"{ticker}: {ratio:.0%} of {len(CONCEPT_NAMES)} concepts x {years}y — {verdict}"]
    for concept, entry in report.items():
        tags = ", ".join(entry["tags"]) or "—"
        lines.append(f"    {concept:<24} {entry['years']:>2}/{years}y  {tags}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tickers", help="comma-separated tickers; default: EDGAR's own order")
    parser.add_argument("--limit", type=int, default=100, help="how many tickers to ingest")
    parser.add_argument("--years", type=int, default=HISTORY_YEARS)
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--db", default=None, help="override the store path")
    parser.add_argument("--quiet", action="store_true", help="summary only, no per-concept detail")
    args = parser.parse_args(argv)

    try:
        client = SecClient()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.tickers:
        wanted = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        by_ticker = {c.ticker: c for c in companies(client)}
        missing = [t for t in wanted if t not in by_ticker]
        if missing:
            print(f"error: not listed in EDGAR: {', '.join(missing)}", file=sys.stderr)
            return 2
        targets = [by_ticker[t] for t in wanted]
    else:
        targets = companies(client)[: args.limit]

    conn = db.connect(args.db)
    failures: list[str] = []
    for company in targets:
        try:
            rows = ingest(conn, client, company.ticker, company.cik)
        except SecRequestError as exc:
            # One unreachable company must not abort a 100-company run, but it is
            # reported rather than silently counted as coverage of zero.
            failures.append(f"{company.ticker}: {exc}")
            continue
        print(f"{company.ticker:<8} cik={company.cik:<10} {rows:>5} facts")

    print()
    excluded = 0
    for company in targets:
        report = db.coverage(conn, company.ticker, args.as_of, args.years)
        if db.coverage_ratio(report, args.years) < MIN_CONCEPT_COVERAGE:
            excluded += 1
        lines = coverage_lines(conn, company.ticker, args.as_of, args.years)
        print(lines[0] if args.quiet else "\n".join(lines))

    print(f"\ningested {len(targets) - len(failures)}/{len(targets)} companies; "
          f"{excluded} below the {MIN_CONCEPT_COVERAGE:.0%} coverage floor")
    for failure in failures:
        print(f"  failed: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

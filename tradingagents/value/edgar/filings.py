"""10-K narrative sections: locate the filing, strip the HTML, extract Items 1, 1A and 7.

Section extraction is load-bearing rather than an optimisation (plan section 8).
A full 10-K is routinely 300k+ tokens and DeepSeek's context is a fraction of
that, so tier 3 is fed three sections or it is fed nothing.

Two things make this harder than a regex over headings:

- the table of contents names every item before the body does, so the *first*
  match for "Item 1A" is almost always a one-line TOC entry. A section body is
  therefore taken from the longest span a marker opens, not the first;
- item numbering varies by filer and by year (1B and 1C are recent additions,
  7A is sometimes absent), so a section ends at whichever item marker comes
  next, whatever its number.

ponytail: a heuristic, not a parser. It comes back short on filings that
incorporate a section by reference to an exhibit or a proxy; that shows up in
``Sections.missing`` rather than as a confident fragment, and the caller decides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from parsel import Selector

from ..config import SECTION_TOKEN_BUDGET
from .client import SecClient

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

# Item number -> the field it lands in. Ordered as the filing orders them.
_TARGETS = (("1", "business"), ("1A", "risk_factors"), ("7", "mdna"))

_ITEM_RE = re.compile(r"\bitem\s+(\d{1,2}[abc]?)\b", re.IGNORECASE)

# A filing cites its own items constantly — `Item 7, "Management's Discussion
# and Analysis…"` appears eight times inside DECK's Item 1 alone. Those citations
# broke extraction in both directions: one opened a 22k-character span that was
# really the tail of Item 1 and won on length, and another closed the real MD&A
# after 1,260 characters. The analyst caught it and said so in `evidence_gaps`
# ("reads as Item 1 operational text rather than MD&A"), which is the only reason
# it was ever noticed.
#
# A heading introduces its title — `Item 7. MANAGEMENT'S DISCUSSION` — while a
# citation continues a sentence. Punctuation is what separates them, and filers
# punctuate in two different places:
#
#   DECK  Part II, Item 7, "Management's Discussion…"   -> comma *after* the number
#   KO    in Part I, "Item 1. Business" of this report  -> quote *before* it
#
# KO's form is the reason this needs both tests. Its number sits inside the
# quotation marks, so the character after it is the same full stop a real heading
# uses, and an after-only rule reads every citation in the filing as a heading.
_XREF_AFTER_RE = re.compile(
    r"""\s*(?:[,;)"'”’]                                     # Item 7, "Management's…"
        |(?:of|in|to|above|below|under|and|or|herein|hereof|thereof)\b)""",
    re.IGNORECASE | re.VERBOSE,
)
_QUOTE_OPENERS = ('"', "'", "“", "‘")


def _is_citation(text: str, start: int, end: int) -> bool:
    """Does this ``Item N`` cite a section rather than open one?"""
    if _XREF_AFTER_RE.match(text, end):
        return True
    # Bounded lookback: text is whitespace-collapsed, so the quote is the
    # character before the marker or nothing is. Slicing the whole prefix here
    # would copy the filing once per marker.
    before = text[max(0, start - 4):start].rstrip()
    return bool(before) and before[-1] in _QUOTE_OPENERS

# A section shorter than this is a cross-reference or a stray heading match, not
# a body. Item 1A alone runs to tens of thousands of characters in any real 10-K.
_MIN_SECTION_CHARS = 1_000

# Crude, and deliberately so: no tokenizer dependency for a truncation whose only
# job is to keep the request inside a context window with room to spare. English
# prose runs ~4 characters per token; being wrong by 20% here costs nothing.
_CHARS_PER_TOKEN = 4


class FilingNotFound(LookupError):
    """No 10-K matched the request. Never a reason to fall back to another form."""


@dataclass(frozen=True)
class Sections:
    """The three narrative sections tier 3 reads, already truncated.

    ``dropped`` records the characters truncation removed per section, so a
    thin assessment can be traced to a truncated input rather than guessed at.
    """

    business: str
    risk_factors: str
    mdna: str
    dropped: tuple[tuple[str, int], ...] = ()

    @property
    def missing(self) -> tuple[str, ...]:
        """Sections extraction could not find. Empty is the expected case."""
        return tuple(
            name
            for name in ("business", "risk_factors", "mdna")
            if not getattr(self, name)
        )


@dataclass(frozen=True)
class Filing:
    """One 10-K as EDGAR identifies it, with its primary document's HTML."""

    cik: int
    accession: str
    filed: str
    url: str
    html: str


def to_text(html: str) -> str:
    """Visible text of a filing document, whitespace collapsed."""
    selector = Selector(text=html)
    selector.css("script, style").drop()
    return _collapse(" ".join(selector.xpath("//text()").getall()))


def extract(text: str, token_budget: int = SECTION_TOKEN_BUDGET) -> Sections:
    """Pull Items 1, 1A and 7 out of a stripped 10-K and truncate each.

    Citations are dropped before any span is measured, so they neither open a
    section nor close one. Dropping them in only one direction would trade this
    filing's wrong section for the next filing's truncated one.
    """
    markers = [(match.start(), match.end(), match.group(1).upper())
               for match in _ITEM_RE.finditer(text)
               if not _is_citation(text, match.start(), match.end())]

    bodies: dict[str, str] = {}
    dropped: list[tuple[str, int]] = []
    limit = token_budget * _CHARS_PER_TOKEN
    for item, field in _TARGETS:
        body = _longest_span(text, markers, item)
        if len(body) > limit:
            dropped.append((field, len(body) - limit))
            body = body[:limit]
        bodies[field] = body

    return Sections(dropped=tuple(dropped), **bodies)


def fetch_10k(client: SecClient, cik: int, as_of: str | None = None) -> Filing:
    """The most recent 10-K filed **on or before** ``as_of`` (``YYYY-MM-DD``).

    Filtering on the filing date, not the period end, is the same point-in-time
    rule the facts store follows: a filing published after the as-of date did not
    exist for a decision made on it.
    """
    payload = client.get_json(SUBMISSIONS_URL.format(cik=cik))
    recent = payload.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    documents = recent.get("primaryDocument", [])

    best: tuple[str, str, str] | None = None
    # strict: the four arrays are parallel by EDGAR's own contract. A ragged
    # payload means the schema moved, which is worth raising over rather than
    # silently reading a truncated filing history.
    rows = zip(forms, dates, accessions, documents, strict=True)
    for form, filed, accession, document in rows:
        if form != "10-K" or not document:
            continue
        if as_of is not None and filed > as_of:
            continue
        if best is None or filed > best[0]:
            best = (filed, accession, document)

    if best is None:
        # ponytail: only the ``recent`` page is read — roughly the last thousand
        # filings, which covers a decade of 10-Ks for any normal filer. A
        # backtest reaching further back would need the paginated ``files`` list.
        raise FilingNotFound(
            f"No 10-K for CIK {cik} on or before {as_of or 'today'} in EDGAR's recent filings"
        )

    filed, accession, document = best
    url = ARCHIVE_URL.format(cik=cik, accession=accession.replace("-", ""), document=document)
    return Filing(cik=cik, accession=accession, filed=filed, url=url, html=client.get(url).text)


def sections_for(
    client: SecClient,
    cik: int,
    as_of: str | None = None,
    token_budget: int = SECTION_TOKEN_BUDGET,
) -> tuple[Filing, Sections]:
    """Fetch and extract in one step; the filing is returned so the date travels with it."""
    filing = fetch_10k(client, cik, as_of)
    return filing, extract(to_text(filing.html), token_budget)


def _longest_span(text: str, markers: list[tuple[int, int, str]], item: str) -> str:
    """Body of ``item``: the longest run from one of its markers to the next item.

    Longest, because the table of contents opens the same item a few characters
    before the next one and the body opens it pages before.
    """
    spans = []
    for index, (_, end, label) in enumerate(markers):
        if label != item:
            continue
        stop = len(text)
        for start, _, other in markers[index + 1:]:
            if other != item:
                stop = start
                break
        spans.append(text[end:stop].strip(" .:—-"))

    if not spans:
        return ""
    body = max(spans, key=len)
    return body if len(body) >= _MIN_SECTION_CHARS else ""


def _collapse(text: str) -> str:
    return re.sub(r"[\s ]+", " ", text).strip()

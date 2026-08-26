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

# The third form, and the one that survived the first two. Neither punctuation
# test fires when a filer writes the citation as plain prose:
#
#   LMT  ...notes thereto included in Item 8 - Financial Statements…
#   LMT  For additional information…, see Item 1A - Risk Factors…
#   HD   ...consolidated financial statements and related notes and Part II, Item 7.
#   ITW  Refer to Item 7. Management's Discussion and Analysis…
#   JNJ  …incorporated herein by reference to the material under the captions Item 1.
#
# Every one of those is followed by a dash or a full stop — exactly what a real
# heading looks like — so what separates them sits *before* the number. A heading
# follows the end of something: a full stop, a page number, "Table of Contents".
# A prose citation follows the preposition or verb that governs it.
#
# Matched at the end of a bounded lookback rather than by enumerating headings,
# because the default has to stay "this opens a section". An opener wrongly
# dropped truncates the section before it, which is the failure the after-rule's
# comment above warns about, and it is the more expensive of the two.
_XREF_BEFORE_RE = re.compile(
    r"""(?:\b(?:see|refer(?:s|red|ring)?\s+to|described\s+in|set\s+forth\s+in
        |included\s+in|contained\s+in|discussed\s+in|incorporated\s+(?:by\s+reference\s+)?in
        # JNJ's Part III points at the proxy this way, and the word that governs
        # the number is the noun rather than the preposition before it.
        |caption(?:s)?|heading(?:s)?|entitled
        |in|into|under|within|to|from|with|of|and|or|per|through|by|and\s+in)
        \s*[:;,]?\s*                                     # "under: Item 7."
        (?:part\s+[ivx]+\s*[,.]?\s*)?)$""",
    re.IGNORECASE | re.VERBOSE,
)

# "Refer to Note 3. Divestitures in Item 8." — the governing verb is five words
# back, not one. Long enough to reach it and the "Part II," that may sit between,
# short enough that slicing it per marker does not copy the filing.
_LOOKBACK_CHARS = 48


def _is_citation(text: str, start: int, end: int) -> bool:
    """Does this ``Item N`` cite a section rather than open one?"""
    if _XREF_AFTER_RE.match(text, end):
        return True
    # Bounded lookback: text is whitespace-collapsed, so the quote is the
    # character before the marker or nothing is. Slicing the whole prefix here
    # would copy the filing once per marker.
    before = text[max(0, start - _LOOKBACK_CHARS):start].rstrip()
    if not before:
        return False
    return before[-1] in _QUOTE_OPENERS or bool(_XREF_BEFORE_RE.search(before))

# A section shorter than this is a cross-reference or a stray heading match, not
# a body. Item 1A alone runs to tens of thousands of characters in any real 10-K.
_MIN_SECTION_CHARS = 1_000

# A found section this many times smaller than the largest found section is a
# fragment wearing a body's clothes. KO's broken MD&A ran 3,199 characters while
# its risk_factors span held 96k of MD&A text — a 30x spread. Real 10-Ks spread
# maybe 3-4x between Items 1, 1A and 7, so 20 leaves room for an odd filer and
# still catches the failure this exists for.
_SIZE_OUTLIER_RATIO = 20

# MD&A runs until Item 7A or Item 8, and Item 7A is short, so the unclaimed text
# between the end of the extracted MD&A and the start of Item 8's body should be
# a small fraction of the MD&A itself. Measured over the six filings the original
# defect was found on: with extraction correct the ratio is 0.04-0.18; with the
# defect re-created it is 0.89, 2.82, 2.98 and 91.55 for the four that close
# early. 0.5 sits in the empty middle with margin on both sides.
_COVERAGE_GAP_RATIO = 0.5

# Item 8 is not extracted — it is the financial statements, which tier 3 does not
# read — but its body is where MD&A must stop, so it is located for that purpose
# alone. The other two sections have no equally clean terminator: Item 1A ends at
# 1B or 1C or 2 depending on the filer and the year, so no coverage rule is
# applied to them rather than one guessed at.
_MDNA_TERMINATOR = "8"

# The mirror of the coverage rule, for a span that opens late instead of closing
# early. Items 1B through 6 sit between Risk Factors and MD&A and are short, so
# the run-up to MD&A is a fraction of MD&A itself. Same six filings: 0.16-0.39
# when extraction is correct, 1.02-5.17 for the four broken ones that open late.
# 0.7 sits between, though on six filings that margin is thinner than the
# coverage rule's — a filer with an unusually long Item 2 could trip it. The cost
# of being wrong here is a briefing that arrives carrying a caveat, never one
# that does not arrive.
_LEAD_IN_RATIO = 0.7

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
    # Structural complaints about the spans these bodies came from. Empty is the
    # expected case; anything here means the extraction is probably wrong even
    # though it returned text. See ``_geometry_faults``.
    suspect: tuple[str, ...] = ()

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
    bounds: dict[str, tuple[int, int]] = {}
    sizes: dict[str, int] = {}
    dropped: list[tuple[str, int]] = []
    limit = token_budget * _CHARS_PER_TOKEN
    for item, field in _TARGETS:
        span = _longest_span(text, markers, item)
        if span is None:
            bodies[field] = ""
            continue
        bounds[field] = span
        body = text[span[0]:span[1]].strip(" .:—-")
        # Measured before truncation: the budget shortens every long section
        # equally, so comparing post-truncation lengths would hide the outlier.
        sizes[field] = len(body)
        if len(body) > limit:
            dropped.append((field, len(body) - limit))
            body = body[:limit]
        bodies[field] = body

    return Sections(
        dropped=tuple(dropped),
        suspect=_geometry_faults(
            bounds, sizes, _longest_span(text, markers, _MDNA_TERMINATOR)
        ),
        **bodies,
    )


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


def _longest_span(
    text: str, markers: list[tuple[int, int, str]], item: str
) -> tuple[int, int] | None:
    """Bounds of ``item``'s body: the longest run from one of its markers to the next.

    Longest, because the table of contents opens the same item a few characters
    before the next one and the body opens it pages before.

    Bounds rather than the text itself, because where a span sits is what
    ``_geometry_faults`` checks — two sections cannot both be correct and also
    overlap, and that is not visible once the slices are separate strings.
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
        spans.append((end, max(end, stop)))

    if not spans:
        return None
    start, stop = max(spans, key=lambda bound: bound[1] - bound[0])
    return (start, stop) if stop - start >= _MIN_SECTION_CHARS else None


def _geometry_faults(
    bounds: dict[str, tuple[int, int]],
    sizes: dict[str, int],
    terminator: tuple[int, int] | None = None,
) -> tuple[str, ...]:
    """Ways the extracted spans contradict how a 10-K is laid out.

    Items 1, 1A and 7 appear once each, in that order, and do not contain one
    another. A run that violates that returned text for every section and was
    still wrong — which is exactly the failure mode nobody could see: five of six
    filings extracted the wrong MD&A and every one of them looked plausible.

    Complaints, not exceptions. A filer odd enough to trip this should reach the
    operator as a flagged briefing, not as a missing one.
    """
    faults: list[str] = []
    ordered = sorted(bounds.items(), key=lambda kv: kv[1][0])

    expected = [field for _item, field in _TARGETS if field in bounds]
    actual = [field for field, _bound in ordered]
    if actual != expected:
        faults.append(
            "sections are out of filing order: found " + " -> ".join(actual)
            + ", expected " + " -> ".join(expected)
        )

    for (left, (_, left_stop)), (right, (right_start, _)) in zip(ordered, ordered[1:], strict=False):
        if right_start < left_stop:
            faults.append(
                f"{left} and {right} overlap by {left_stop - right_start:,} characters — "
                f"one of them is holding the other's text"
            )

    if len(sizes) > 1:
        largest = max(sizes.values())
        for field, size in sizes.items():
            if size * _SIZE_OUTLIER_RATIO < largest:
                faults.append(
                    f"{field} is {size:,} characters against {largest:,} for the largest "
                    "section — too small to be a body"
                )

    # Coverage: does MD&A actually reach the financial statements? A section
    # closed early by a citation to a later item returns real text from the right
    # place and is the one shape ordering and size both miss.
    mdna = bounds.get("mdna")
    if mdna is not None and terminator is not None and sizes.get("mdna"):
        gap = terminator[0] - mdna[1]
        if gap > sizes["mdna"] * _COVERAGE_GAP_RATIO:
            faults.append(
                f"mdna stops {gap:,} characters short of Item 8, against a body of "
                f"{sizes['mdna']:,} — it is closing early"
            )

    # And the same question at the other end: a span that opens inside a citation
    # reaches Item 8 correctly and is the right length to pass the size rule, so
    # only the run-up gives it away.
    risk = bounds.get("risk_factors")
    if mdna is not None and risk is not None and sizes.get("mdna"):
        lead = mdna[0] - risk[1]
        if lead > sizes["mdna"] * _LEAD_IN_RATIO:
            faults.append(
                f"mdna starts {lead:,} characters after Item 1A ends, against a body of "
                f"{sizes['mdna']:,} — it is opening late"
            )
    return tuple(faults)


def _collapse(text: str) -> str:
    return re.sub(r"[\s ]+", " ", text).strip()

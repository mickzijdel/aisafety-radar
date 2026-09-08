"""Check an extraction back against the text it came from, and repair what fails.

Nothing here asks a model whether a model was right. Every check is a normalised substring test
against the document (and, where the claim is about the discussion, the comments), so the failure
mode is a false negative -- a real quote dropped because the source spells it differently -- rather
than a hallucination waved through. Quotes, the author's stated status and deadlines are repaired
in place because a wrong one is worth less than none; unverifiable numbers and dates in the prose
are reported instead, because only the caller can decide whether to retry or degrade.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from pipeline.models import Extraction
from pipeline.textnorm import contains, find_numbers_and_dates


@dataclass
class VerifyResult:
    ok: bool
    dropped_quotes: list[str] = field(default_factory=list)
    bad_tokens: list[str] = field(default_factory=list)
    deadline_dropped: bool = False
    author_status_dropped: bool = False


class ExtractionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extraction: Extraction
    degraded: bool = False
    notes: list[str] = []


def verify(
    extraction: Extraction, source_text: str, comments_text: str = ""
) -> tuple[Extraction, VerifyResult]:
    """Return the extraction with unverifiable quotes and deadline removed, plus what failed.

    `source_text` must be plain text from `html_to_text`, not markdown: see `contains`. Quotes and
    `author_stated_status` are checked against the document alone, since both are presented as the
    author's own words and a commenter's are not. Everything else may draw on the comments too.

    `author_stated_status` is verified for the same reason a quote is, and it is easy to miss why:
    `item.j2` renders it inside quotation marks as `Author's stated status: "..."`, and spec 4.3
    calls it verbatim. Unverified, an invented "Epistemic status: 95% confident, replicated 3
    times" reached the pack as something the author had said about their own confidence -- the one
    field a reader would weigh a claim by.
    """
    corpus = source_text + "\n" + comments_text
    result = VerifyResult(ok=True)
    quotes = []
    for q in extraction.key_quotes:
        if contains(source_text, q):
            quotes.append(q)
        else:
            result.dropped_quotes.append(q)
    status = extraction.author_stated_status
    if status and not contains(source_text, status):
        status = None
        result.author_status_dropped = True
    deadline = extraction.deadline
    if deadline and not contains(corpus, deadline):
        deadline = None
        result.deadline_dropped = True
    for text in (extraction.summary, extraction.novelty, extraction.discussion_signal):
        for tok in find_numbers_and_dates(text or ""):
            if not contains(corpus, tok):
                result.bad_tokens.append(tok)
    # One wrong number repeated across summary and novelty is one problem, and the retry feedback
    # that names it should say it once. `dict.fromkeys` keeps first-seen order.
    result.bad_tokens = list(dict.fromkeys(result.bad_tokens))
    # A dropped quote, status or deadline is already repaired, so only the prose tokens -- which
    # the caller can do nothing about except ask again -- decide whether this passed.
    result.ok = not result.bad_tokens
    cleaned = extraction.model_copy(
        update={"key_quotes": quotes, "deadline": deadline, "author_stated_status": status}
    )
    return cleaned, result

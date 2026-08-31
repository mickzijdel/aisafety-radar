from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup
from markdownify import markdownify

_QUOTES = {"‘": "'", "’": "'", "“": '"', "”": '"', "′": "'", "″": '"'}
# U+2010 hyphen, U+2011 non-breaking hyphen, U+2012 figure dash, U+2013 en dash,
# U+2014 em dash, U+2212 minus sign.
_DASHES = {"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"}
# Soft hyphen, zero-width space/non-joiner/joiner, and BOM. These survive NFKC but
# would break a substring test, so they are deleted outright.
_INVISIBLE = "­​‌‍﻿"
_FOLD = str.maketrans({**_QUOTES, **_DASHES, **dict.fromkeys(_INVISIBLE)})
_WS = re.compile(r"\s+")

# Tags whose boundaries are a word break in the rendered text. Everything else is
# inline and must be joined with no separator, or "<strong>45</strong>%" becomes "45 %".
_BLOCK_TAGS = [
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
]

# A token may not start glued to a preceding word character, dot or hyphen, or
# "GPT-4o", "v1.2.3" and "COVID19" shed number fragments that verify against nothing.
_LEFT = r"(?<![\w.-])"
# ...nor end glued to a following word character, or "5km" sheds a bare "5".
_RIGHT = r"(?!\w)"

# Explicit month names, longest form first so "January" is not truncated to "Jan".
# A bare "[a-z]*" suffix would turn "Marketing 2026" and "Mayor 2026" into dates.
_MONTH_NAMES = (
    "January|Jan|February|Feb|March|Mar|April|Apr|May|June|Jun|July|Jul|"
    "August|Aug|September|Sept|Sep|October|Oct|November|Nov|December|Dec"
)
# Case-insensitive only for the month, so "29 august 2026" is one token while the
# number suffixes below keep their own explicit casing.
_MONTH = rf"\b(?i:{_MONTH_NAMES})\b\.?"

_NUMBER = rf"{_LEFT}\$?\d(?:[\d,]*\d)?(?:\.\d+)?(?:%|[xX]|[kKmMbB]\b)?{_RIGHT}"
_DATE_ISO = rf"{_LEFT}\d{{4}}-\d{{2}}-\d{{2}}"
_DATE_DMY = rf"{_LEFT}\d{{1,2}} {_MONTH} \d{{4}}"
_DATE_MDY = rf"{_MONTH} \d{{1,2}}(?:,? \d{{4}})?"
_DATE_MY = rf"{_MONTH} \d{{4}}"
# Order matters: _DATE_MY must precede _DATE_MDY or "Aug 2026" matches as "Aug 20",
# and every date must precede _NUMBER or "29 August 2026" splits into "29" and "2026".
_TOKEN_RE = re.compile(rf"({_DATE_ISO}|{_DATE_DMY}|{_DATE_MY}|{_DATE_MDY}|{_NUMBER})")


def _strip_dead_tags(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup


def html_to_text(html: str) -> str:
    """Render HTML to plain text, preserving inline word boundaries exactly."""
    soup = _strip_dead_tags(html)
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    return _WS.sub(" ", soup.get_text()).strip()


def html_to_markdown(html: str) -> str:
    return markdownify(str(_strip_dead_tags(html)), heading_style="ATX").strip()


def normalize(s: str) -> str:
    # Fold before NFKC: NFKC decomposes U+2033 into two primes, which would defeat
    # the double-prime mapping above.
    return _WS.sub(" ", unicodedata.normalize("NFKC", s.translate(_FOLD))).strip()


def contains(source: str, needle: str) -> bool:
    """Is `needle` a substring of `source` once both are normalised?

    `source` must be plain text, normally from `html_to_text`. Markdown from
    `html_to_markdown` is not a valid source: it escapes metacharacters, so a
    quote that is present in the prose can fail to match.
    """
    return normalize(needle) in normalize(source)


def find_numbers_and_dates(text: str) -> list[str]:
    """Return every number or date token, longest match first at each position."""
    return [m.group(1) for m in _TOKEN_RE.finditer(text)]


def truncate_tokens_approx(text: str, max_tokens: int) -> str:
    return text[: max(max_tokens, 0) * 4]


# U+2039 and U+203A: single angle quotation marks. They read as angle brackets to a model but are
# not the characters our prompt delimiters are built from.
_ANGLE_BRACKETS = str.maketrans({"<": "‹", ">": "›"})


def neutralize_angle_brackets(s: str) -> str:
    """Replace `<` and `>` so untrusted text cannot forge a prompt data-block delimiter.

    Every prompt that wraps fetched text in an `<item>...</item>` block tells the model the block
    is untrusted. That framing is worth nothing if the text can write `</item>` itself: everything
    after it would appear to sit outside the block, where the model reads it as instructions
    rather than as data. Interpolate nothing untrusted into a prompt without this.

    A quote the model copies out of neutralised text may then fail `contains` against the original
    source, which drops the quote. That is the safe direction to fail in.
    """
    return s.translate(_ANGLE_BRACKETS)


# An attribute value sits inside our own open tag, so it must lose more than the brackets: a bare
# `"` closes the value and lets the rest forge further attributes (`id="x" trusted="yes`), and a
# bare `&` could reintroduce any of these as an entity. U+201D right double quotation mark and
# U+FF06 fullwidth ampersand read as themselves to a model but are not the characters a delimiter
# or an entity is built from.
_ATTR_VALUE = str.maketrans({"<": "‹", ">": "›", '"': "”", "&": "＆"})


def data_block(tag: str, attrs: dict[str, str], body: str) -> str:
    """Render one untrusted-data block: `<tag k="v" ...>`, the body, then `</tag>`.

    Every prompt that hands the model fetched text does it through here, so no call site has to
    remember to neutralise. Attribute values and body are both remote-derived: a value is stripped
    of quotes and ampersands as well as brackets, since it sits inside our own tag, while the body
    only has to lose its brackets. `tag` and the attribute names are ours and are interpolated as
    written. Every substitution is 1:1 per character, so a caller that truncated the body to a
    character budget still fits inside it however bracket-dense the text was.
    """
    rendered = "".join(f' {k}="{v.translate(_ATTR_VALUE)}"' for k, v in attrs.items())
    return f"<{tag}{rendered}>\n{neutralize_angle_brackets(body)}\n</{tag}>"

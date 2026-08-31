from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup
from markdownify import markdownify

_QUOTES = {"‘": "'", "’": "'", "“": '"', "”": '"', "′": "'"}
_DASHES = {"–": "-", "—": "-", "−": "-", "‒": "-"}
_WS = re.compile(r"\s+")

_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_NUMBER = r"\$?\d[\d,]*(?:\.\d+)?(?:%|x|[kKmMbB]\b)?"
_DATE_ISO = r"\d{4}-\d{2}-\d{2}"
_DATE_DMY = rf"\d{{1,2}} {_MONTH} \d{{4}}"
_DATE_MDY = rf"{_MONTH} \d{{1,2}}(?:,? \d{{4}})?"
_DATE_MY = rf"{_MONTH} \d{{4}}"
# Order matters: _DATE_MY must precede _DATE_MDY or "Aug 2026" matches as "Aug 20".
_TOKEN_RE = re.compile(rf"({_DATE_ISO}|{_DATE_DMY}|{_DATE_MY}|{_DATE_MDY}|{_NUMBER})")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return _WS.sub(" ", soup.get_text(" ")).strip()


def html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return markdownify(str(soup), heading_style="ATX").strip()


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    for k, v in {**_QUOTES, **_DASHES}.items():
        s = s.replace(k, v)
    return _WS.sub(" ", s).strip()


def contains(source: str, needle: str) -> bool:
    return normalize(needle) in normalize(source)


def find_numbers_and_dates(text: str) -> list[str]:
    """Return every number or date token, longest match first at each position."""
    return [m.group(1) for m in _TOKEN_RE.finditer(text)]


def truncate_tokens_approx(text: str, max_tokens: int) -> str:
    return text[: max_tokens * 4]

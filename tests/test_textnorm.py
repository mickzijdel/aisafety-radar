from pipeline.textnorm import (
    contains,
    find_numbers_and_dates,
    html_to_markdown,
    html_to_text,
    normalize,
    truncate_tokens_approx,
)

HTML = (
    "<h1>Title</h1>"
    '<p>We found a <a href="https://x.example/y">45% drop</a> — see “Figure 2”.</p>'
    "<script>evil()</script>"
)


def test_html_to_text_strips_tags_and_scripts():
    text = html_to_text(HTML)
    assert "Title" in text and "45% drop" in text
    assert "evil" not in text and "<a" not in text


def test_html_to_markdown_keeps_links():
    md = html_to_markdown(HTML)
    assert "[45% drop](https://x.example/y)" in md


def test_normalize_quotes_dashes_whitespace():
    assert normalize("“Figure  2” — it’s") == '"Figure 2" - it\'s'


def test_contains_is_normalised():
    source = html_to_text(HTML)
    assert contains(source, 'see "Figure 2"')
    assert contains(source, "45%   drop")
    assert not contains(source, "46% drop")


def test_find_numbers_and_dates():
    found = find_numbers_and_dates(
        "On 2026-08-29 and 29 August 2026, karma rose 25% to 145; cost $1.5M, 10x faster, Aug 2026."
    )
    for tok in ["2026-08-29", "29 August 2026", "25%", "145", "$1.5M", "10x", "Aug 2026"]:
        assert tok in found, (tok, found)


def test_truncate_tokens_approx():
    assert len(truncate_tokens_approx("a" * 1000, 10)) == 40

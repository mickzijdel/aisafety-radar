import pytest
from pydantic import ValidationError

from pipeline.models import Extraction
from pipeline.stages.verify import ExtractionRecord, VerifyResult, verify

SOURCE = 'We measured a 45% drop on 29 August 2026 across 12 models. "Figure 2" shows it.'


def ex(**over):
    base = dict(
        item_id="x",
        summary="A 45% drop across 12 models on 2026-08-29.",
        key_quotes=[],
        author_stated_status=None,
        claim_type="confident-empirical",
        novelty=None,
        discussion_signal=None,
        deadline=None,
        entities=[],
        slugs=["evals"],
        importance=4,
    )
    base.update(over)
    return Extraction(**base)


def test_quotes_verified_and_dropped():
    e, r = verify(ex(key_quotes=['"Figure 2" shows it.', "we found nothing"]), SOURCE)
    assert e.key_quotes == ['"Figure 2" shows it.'] and r.dropped_quotes == ["we found nothing"]


def test_numbers_and_dates_in_prose_must_exist():
    e, r = verify(ex(summary="A 45% drop across 12 models on 29 August 2026."), SOURCE)
    assert r.ok and r.bad_tokens == []
    e, r = verify(ex(summary="A 46% drop across 12 models."), SOURCE)
    assert not r.ok and r.bad_tokens == ["46%"]


def test_iso_date_not_in_source_is_bad():
    _, r = verify(ex(), SOURCE)
    assert r.bad_tokens == ["2026-08-29"]


def test_deadline_verified():
    e, r = verify(ex(summary="ok", deadline="15 September 2026"), SOURCE)
    assert e.deadline is None and r.deadline_dropped


def test_comments_text_counts_as_source_for_discussion_signal():
    e, r = verify(
        ex(summary="ok", discussion_signal="Top comment (83 karma) disagrees."),
        SOURCE,
        comments_text="Bob (83 karma): I disagree",
    )
    assert r.ok


def test_a_deadline_present_in_the_comments_survives():
    """The corpus for the deadline is source plus comments, not the source alone."""
    e, r = verify(
        ex(summary="ok", deadline="15 September 2026"),
        SOURCE,
        comments_text="Applications close 15 September 2026.",
    )
    assert e.deadline == "15 September 2026" and not r.deadline_dropped


def test_quotes_are_checked_against_the_source_only_not_the_comments():
    """A quote is presented as the document's words; a commenter's words are not the author's."""
    e, r = verify(
        ex(summary="ok", key_quotes=["I disagree"]),
        SOURCE,
        comments_text="Bob (83 karma): I disagree",
    )
    assert e.key_quotes == [] and r.dropped_quotes == ["I disagree"]


def test_verify_does_not_mutate_the_extraction_it_was_given():
    original = ex(key_quotes=["we found nothing"], deadline="15 September 2026")
    cleaned, _ = verify(original, SOURCE)
    assert original.key_quotes == ["we found nothing"]
    assert original.deadline == "15 September 2026"
    assert cleaned is not original


def test_dropped_quotes_alone_do_not_fail_verification():
    """`ok` tracks unverifiable numbers and dates; a dropped quote is already repaired."""
    e, r = verify(ex(summary="ok", key_quotes=["never said this"]), SOURCE)
    assert r.ok and r.dropped_quotes == ["never said this"] and e.key_quotes == []


def test_bad_tokens_are_collected_from_novelty_and_discussion_signal_too():
    _, r = verify(
        ex(summary="ok", novelty="first 99% result", discussion_signal="7 commenters agree"),
        SOURCE,
    )
    assert r.bad_tokens == ["99%", "7"] and not r.ok


def test_normalisation_applies_so_a_typographic_quote_still_verifies():
    """`contains` normalises both sides, so curly quotes and en dashes do not fail an item."""
    e, r = verify(
        ex(summary="ok", key_quotes=['"Figure 2" shows it.']),
        "We measured a 45% drop. “Figure 2” shows it.",
    )
    assert e.key_quotes == ['"Figure 2" shows it.'] and r.dropped_quotes == []


def test_verify_result_defaults_to_a_clean_pass():
    r = VerifyResult(ok=True)
    assert r.dropped_quotes == [] and r.bad_tokens == [] and r.deadline_dropped is False


def test_extraction_record_defaults_and_forbids_unknown_fields():
    rec = ExtractionRecord(extraction=ex())
    assert rec.degraded is False and rec.notes == []
    with pytest.raises(ValidationError):
        ExtractionRecord(extraction=ex(), bogus=1)


def test_extraction_record_notes_are_not_shared_between_instances():
    a = ExtractionRecord(extraction=ex())
    a.notes.append("one")
    assert ExtractionRecord(extraction=ex()).notes == []

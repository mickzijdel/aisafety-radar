import json
from datetime import UTC, date, datetime

import pytest

from pipeline import ledger as ledger_module
from pipeline.archive import Archive
from pipeline.models import Comment, Extraction
from pipeline.stages.verify import ExtractionRecord
from tests.test_models import make_item


def rec(item_id, summary="s"):
    return ExtractionRecord(
        extraction=Extraction(
            item_id=item_id,
            summary=summary,
            key_quotes=[],
            author_stated_status=None,
            claim_type="exploratory",
            novelty=None,
            discussion_signal=None,
            deadline=None,
            entities=[],
            slugs=["evals"],
            importance=2,
        )
    )


def make_comment(**over):
    base = dict(
        id="lw:c1",
        post_id="lw:1",
        site="lw",
        author="A",
        author_created_at=None,
        posted_at=datetime(2026, 8, 30, tzinfo=UTC),
        score=4,
        agreement=None,
        parent_id=None,
        text="body",
    )
    base.update(over)
    return Comment(**base)


def _boom(_src, _dst):
    """Stand-in for os.replace that fails the way a full disk would."""
    raise OSError("disk full")


def test_items_roundtrip_and_idempotent(tmp_path):
    a = Archive(tmp_path)
    d = date(2026, 8, 30)
    a.write_items(d, [make_item(id="lw:1"), make_item(id="lw:2")])
    a.write_items(d, [make_item(id="lw:2", title="changed"), make_item(id="lw:3")])
    items = a.read_items(d)
    assert [i.id for i in items] == ["lw:1", "lw:2", "lw:3"]
    assert items[1].title == "changed"
    assert (tmp_path / "2026" / "08" / "30" / "items.jsonl").exists()


def test_extractions_update_and_find(tmp_path):
    a = Archive(tmp_path)
    a.write_extractions(date(2026, 8, 28), [rec("lw:old")])
    a.write_extractions(date(2026, 8, 30), [rec("lw:new")])
    a.update_extraction(date(2026, 8, 28), rec("lw:old", summary="refreshed"))
    found = a.find_extraction("lw:old", date(2026, 8, 30))
    assert found is not None and found[0] == date(2026, 8, 28)
    assert found[1].extraction.summary == "refreshed"
    assert a.find_extraction("lw:missing", date(2026, 8, 30)) is None
    assert a.days_with_extractions(date(2026, 8, 31), 7) == [date(2026, 8, 28), date(2026, 8, 30)]


def test_run_json_and_paths(tmp_path):
    a = Archive(tmp_path)
    a.write_run(date(2026, 8, 30), {"version": "2026.08.30a"})
    assert (a.day_dir(date(2026, 8, 30)) / "run.json").read_text().startswith("{")
    assert a.ledger_path == tmp_path / "posts-ledger.json"


def test_seen_path_sits_beside_the_ledger(tmp_path):
    assert Archive(tmp_path).seen_path == tmp_path / "seen-ids.json"


def test_day_dir_zero_pads_month_and_day(tmp_path):
    assert Archive(tmp_path).day_dir(date(2026, 1, 5)) == tmp_path / "2026" / "01" / "05"


def test_reads_of_a_day_never_written_are_empty(tmp_path):
    a = Archive(tmp_path)
    assert a.read_items(date(2026, 8, 30)) == []
    assert a.read_extractions(date(2026, 8, 30)) == []
    assert a.days_with_extractions(date(2026, 8, 31), 7) == []


def test_update_extraction_replaces_one_record_and_keeps_the_others(tmp_path):
    a = Archive(tmp_path)
    d = date(2026, 8, 30)
    a.write_extractions(d, [rec("lw:1"), rec("lw:2"), rec("lw:3")])
    a.update_extraction(d, rec("lw:2", summary="refreshed"))
    records = a.read_extractions(d)
    assert [r.extraction.item_id for r in records] == ["lw:1", "lw:2", "lw:3"]
    assert [r.extraction.summary for r in records] == ["s", "refreshed", "s"]


def test_comments_merge_by_id_across_reruns(tmp_path):
    a = Archive(tmp_path)
    d = date(2026, 8, 30)
    a.write_comments(d, [make_comment(id="lw:c1"), make_comment(id="lw:c2")])
    a.write_comments(d, [make_comment(id="lw:c2", score=99), make_comment(id="lw:c3")])
    rows = [
        json.loads(line)
        for line in (a.day_dir(d) / "comments.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert [r["id"] for r in rows] == ["lw:c1", "lw:c2", "lw:c3"]
    assert rows[1]["score"] == 99


def test_run_json_replaces_the_previous_run_and_serialises_dates(tmp_path):
    a = Archive(tmp_path)
    d = date(2026, 8, 30)
    a.write_run(d, {"version": "a", "statuses": []})
    a.write_run(d, {"version": "b", "generated_at": datetime(2026, 8, 30, 6, tzinfo=UTC)})
    payload = json.loads((a.day_dir(d) / "run.json").read_text())
    assert payload["version"] == "b"
    assert "statuses" not in payload
    assert payload["generated_at"].startswith("2026-08-30 06:00")


def test_find_extraction_prefers_the_most_recent_day(tmp_path):
    a = Archive(tmp_path)
    a.write_extractions(date(2026, 8, 20), [rec("lw:1", summary="old")])
    a.write_extractions(date(2026, 8, 29), [rec("lw:1", summary="new")])
    found = a.find_extraction("lw:1", date(2026, 8, 30))
    assert found is not None
    assert found[0] == date(2026, 8, 29)
    assert found[1].extraction.summary == "new"


def test_find_extraction_searches_the_day_itself(tmp_path):
    a = Archive(tmp_path)
    a.write_extractions(date(2026, 8, 30), [rec("lw:1")])
    found = a.find_extraction("lw:1", date(2026, 8, 30))
    assert found is not None and found[0] == date(2026, 8, 30)


def test_find_extraction_stops_at_the_lookback_horizon(tmp_path):
    a = Archive(tmp_path)
    a.write_extractions(date(2026, 8, 27), [rec("lw:edge")])
    day = date(2026, 8, 30)
    assert a.find_extraction("lw:edge", day, lookback_days=3) is not None
    assert a.find_extraction("lw:edge", day, lookback_days=2) is None


def test_days_with_extractions_excludes_today_and_days_past_the_window(tmp_path):
    a = Archive(tmp_path)
    for d in (date(2026, 8, 24), date(2026, 8, 29), date(2026, 8, 31)):
        a.write_extractions(d, [rec("lw:1")])
    # Days carrying only items are not days with extractions.
    a.write_items(date(2026, 8, 30), [make_item()])
    assert a.days_with_extractions(date(2026, 8, 31), 5) == [date(2026, 8, 29)]


def test_a_failed_write_keeps_the_previous_file_and_leaves_no_temp_file(tmp_path, monkeypatch):
    a = Archive(tmp_path)
    d = date(2026, 8, 30)
    a.write_items(d, [make_item(id="lw:1")])
    a.write_run(d, {"version": "a"})

    monkeypatch.setattr(ledger_module.os, "replace", _boom)
    with pytest.raises(OSError):
        a.write_items(d, [make_item(id="lw:2")])
    with pytest.raises(OSError):
        a.write_run(d, {"version": "b"})
    monkeypatch.undo()

    assert [i.id for i in a.read_items(d)] == ["lw:1"]
    assert json.loads((a.day_dir(d) / "run.json").read_text()) == {"version": "a"}
    assert sorted(p.name for p in a.day_dir(d).iterdir()) == ["items.jsonl", "run.json"]

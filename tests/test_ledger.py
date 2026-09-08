import json
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from pipeline import ledger as ledger_module
from pipeline.ledger import PostsLedger, SeenIds
from tests.test_models import make_item


def test_seen_ids_roundtrip(tmp_path):
    s = SeenIds(tmp_path / "seen.json").load()
    assert "x" not in s
    s.add("x")
    s.save()
    assert "x" in SeenIds(tmp_path / "seen.json").load()


def test_ledger_snapshot_and_movements(tmp_path):
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    a = make_item(
        id="lw:a", score=40, comment_count=3, published_at=datetime(2026, 8, 25, tzinfo=UTC)
    )
    ledger.snapshot(date(2026, 8, 29), [a], {"lw:a": None})
    a2 = a.model_copy(update={"score": 60, "comment_count": 15})
    ledger.snapshot(date(2026, 8, 30), [a2], {"lw:a": datetime(2026, 8, 30, tzinfo=UTC)})
    ledger.save()

    reloaded = PostsLedger(tmp_path / "ledger.json").load()
    assert reloaded.latest_before(date(2026, 8, 30)) == date(2026, 8, 29)
    moves = reloaded.movements(date(2026, 8, 30))
    assert len(moves) == 1
    assert moves[0].post_id == "lw:a" and moves[0].new_comments == 12
    assert (moves[0].score_before, moves[0].score_after) == (40, 60)


def test_ledger_carries_forward_and_windows(tmp_path):
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    old = make_item(id="lw:old", published_at=datetime(2026, 8, 1, tzinfo=UTC))
    new = make_item(id="lw:new", published_at=datetime(2026, 8, 28, tzinfo=UTC))
    ledger.snapshot(date(2026, 8, 29), [old, new], {})
    ledger.snapshot(date(2026, 8, 30), [], {})
    # Both were fetched on the 29th, so the 29th holds both.
    assert set(ledger.entries(date(2026, 8, 29))) == {"lw:old", "lw:new"}
    # The 30th carries only what is still in the window, so the day's map and the window agree.
    assert set(ledger.entries(date(2026, 8, 30))) == {"lw:new"}
    assert ledger.window_post_ids(date(2026, 8, 30), days=14) == {"lw:new"}


def test_ledger_prunes_old_days(tmp_path):
    ledger = PostsLedger(tmp_path / "ledger.json", keep_days=2).load()
    for d in (28, 29, 30):
        ledger.snapshot(date(2026, 8, d), [], {})
    assert ledger.days() == [date(2026, 8, 29), date(2026, 8, 30)]


# --- seen ids ---------------------------------------------------------------


def test_seen_ids_creates_missing_parent_directory(tmp_path):
    s = SeenIds(tmp_path / "state" / "nested" / "seen.json").load()
    s.add("lw:a")
    s.save()
    assert json.loads((tmp_path / "state" / "nested" / "seen.json").read_text()) == ["lw:a"]


def test_seen_ids_load_on_missing_file_is_empty_and_repeatable(tmp_path):
    s = SeenIds(tmp_path / "seen.json").load()
    assert "anything" not in s
    s.add("lw:a")
    s.add("lw:a")
    s.save()
    reloaded = SeenIds(tmp_path / "seen.json").load()
    assert "lw:a" in reloaded and "lw:b" not in reloaded


def test_seen_ids_save_keeps_previous_file_when_the_rename_fails(tmp_path, monkeypatch):
    path = tmp_path / "seen.json"
    first = SeenIds(path).load()
    first.add("lw:a")
    first.save()

    second = SeenIds(path).load()
    second.add("lw:b")
    monkeypatch.setattr(ledger_module.os, "replace", _boom)
    with pytest.raises(OSError):
        second.save()

    assert json.loads(path.read_text()) == ["lw:a"]
    assert [p.name for p in sorted(tmp_path.iterdir())] == ["seen.json"]


# --- ledger -----------------------------------------------------------------


def test_latest_before_is_none_without_an_earlier_day(tmp_path):
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    ledger.snapshot(date(2026, 8, 30), [make_item(id="lw:a")], {})
    assert ledger.latest_before(date(2026, 8, 30)) is None
    assert ledger.movements(date(2026, 8, 30)) == []


def test_movements_reports_a_first_seen_post_with_no_score_before(tmp_path):
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    ledger.snapshot(date(2026, 8, 29), [make_item(id="lw:a")], {})
    fresh = make_item(id="lw:b", score=12, comment_count=4)
    ledger.snapshot(date(2026, 8, 30), [make_item(id="lw:a"), fresh], {})

    moves = {m.post_id: m for m in ledger.movements(date(2026, 8, 30))}
    assert set(moves) == {"lw:b"}, "an unchanged post is not a movement"
    assert moves["lw:b"].new_comments == 4
    assert (moves["lw:b"].score_before, moves["lw:b"].score_after) == (None, 12)


def test_movements_ignores_carried_forward_entries(tmp_path):
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    ledger.snapshot(date(2026, 8, 29), [make_item(id="lw:a", score=10, comment_count=1)], {})
    ledger.snapshot(date(2026, 8, 30), [], {})

    assert ledger.entries(date(2026, 8, 30))["lw:a"].carried is True
    assert ledger.movements(date(2026, 8, 30)) == []


def test_snapshot_replaces_a_rerun_day_rather_than_merging_with_itself(tmp_path):
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    day = date(2026, 8, 30)
    ledger.snapshot(day, [make_item(id="lw:a", score=10)], {})
    ledger.snapshot(day, [make_item(id="lw:b", score=20)], {})

    assert set(ledger.entries(day)) == {"lw:b"}, "a re-run rebuilds the day from scratch"
    assert ledger.days() == [day]


def test_snapshot_overwrites_a_carried_entry_when_the_post_reappears(tmp_path):
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    ledger.snapshot(date(2026, 8, 29), [make_item(id="lw:a", score=10, comment_count=1)], {})
    ledger.snapshot(date(2026, 8, 30), [], {})
    ledger.snapshot(
        date(2026, 8, 31),
        [make_item(id="lw:a", score=30, comment_count=9)],
        {"lw:a": datetime(2026, 8, 31, 6, tzinfo=UTC)},
    )

    entry = ledger.entries(date(2026, 8, 31))["lw:a"]
    assert entry.carried is False
    assert (entry.score, entry.comment_count) == (30, 9)
    # The comparison day is 8/30, whose entry was itself carried from 8/29.
    moves = ledger.movements(date(2026, 8, 31))
    assert len(moves) == 1
    assert (moves[0].new_comments, moves[0].score_before) == (8, 10)


def test_snapshot_keeps_yesterdays_last_commented_at_when_the_map_omits_the_post(tmp_path):
    """The daily run only builds `last_commented` for the 14-day window posts.

    A post refetched outside that window must not lose the timestamp of its last known comment.
    """
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    commented_at = datetime(2026, 8, 29, 8, tzinfo=UTC)
    ledger.snapshot(date(2026, 8, 29), [make_item(id="lw:a")], {"lw:a": commented_at})
    ledger.snapshot(date(2026, 8, 30), [make_item(id="lw:a", comment_count=9)], {})

    entry = ledger.entries(date(2026, 8, 30))["lw:a"]
    assert entry.last_commented_at == commented_at
    assert entry.carried is False and entry.comment_count == 9


def test_snapshot_last_commented_at_falls_back_across_an_explicit_none(tmp_path):
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    commented_at = datetime(2026, 8, 29, 8, tzinfo=UTC)
    ledger.snapshot(date(2026, 8, 29), [make_item(id="lw:a")], {"lw:a": commented_at})
    # Present-and-None is treated the same as absent: it carries no information to overwrite with.
    ledger.snapshot(date(2026, 8, 30), [make_item(id="lw:a")], {"lw:a": None})

    assert ledger.entries(date(2026, 8, 30))["lw:a"].last_commented_at == commented_at


def test_snapshot_prefers_a_fresh_last_commented_at_over_yesterdays(tmp_path):
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    old = datetime(2026, 8, 29, 8, tzinfo=UTC)
    fresh = datetime(2026, 8, 30, 11, tzinfo=UTC)
    ledger.snapshot(date(2026, 8, 29), [make_item(id="lw:a")], {"lw:a": old})
    ledger.snapshot(date(2026, 8, 30), [make_item(id="lw:a")], {"lw:a": fresh})

    assert ledger.entries(date(2026, 8, 30))["lw:a"].last_commented_at == fresh


def test_snapshot_leaves_last_commented_at_none_for_a_first_seen_post(tmp_path):
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    ledger.snapshot(date(2026, 8, 29), [], {})
    ledger.snapshot(date(2026, 8, 30), [make_item(id="lw:new")], {})

    assert ledger.entries(date(2026, 8, 30))["lw:new"].last_commented_at is None


def test_entries_returns_a_copy(tmp_path):
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    ledger.snapshot(date(2026, 8, 30), [make_item(id="lw:a")], {})

    ledger.entries(date(2026, 8, 30)).clear()
    ledger.entries(date(2026, 8, 31))["lw:ghost"] = object()
    assert set(ledger.entries(date(2026, 8, 30))) == {"lw:a"}
    assert ledger.entries(date(2026, 8, 31)) == {}


def test_save_roundtrips_every_entry_field(tmp_path):
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    item = make_item(
        id="lw:a",
        url="https://www.lesswrong.com/posts/a/x",
        title="Title",
        published_at=datetime(2026, 8, 25, 14, 30, tzinfo=UTC),
        score=40,
        agreement=7,
        comment_count=3,
    )
    commented_at = datetime(2026, 8, 30, 9, 15, tzinfo=UTC)
    ledger.snapshot(date(2026, 8, 30), [item], {"lw:a": commented_at})
    ledger.save()

    entry = PostsLedger(tmp_path / "ledger.json").load().entries(date(2026, 8, 30))["lw:a"]
    assert entry.post_id == "lw:a"
    assert entry.url == "https://www.lesswrong.com/posts/a/x"
    assert entry.title == "Title"
    assert entry.published_at == datetime(2026, 8, 25, 14, 30, tzinfo=UTC)
    assert (entry.score, entry.agreement, entry.comment_count) == (40, 7, 3)
    assert entry.last_commented_at == commented_at
    assert entry.carried is False


def test_save_creates_missing_parent_directory(tmp_path):
    path = tmp_path / "state" / "ledger.json"
    ledger = PostsLedger(path).load()
    ledger.snapshot(date(2026, 8, 30), [make_item(id="lw:a")], {})
    ledger.save()
    assert set(json.loads(path.read_text())["days"]) == {"2026-08-30"}


def test_save_keeps_the_previous_ledger_when_the_rename_fails(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    ledger = PostsLedger(path).load()
    ledger.snapshot(date(2026, 8, 29), [make_item(id="lw:a")], {})
    ledger.save()

    ledger.snapshot(date(2026, 8, 30), [make_item(id="lw:b")], {})
    monkeypatch.setattr(ledger_module.os, "replace", _boom)
    with pytest.raises(OSError):
        ledger.save()

    # A crash mid-write must leave the last good ledger in place, and no debris beside it.
    on_disk = PostsLedger(path).load()
    assert on_disk.days() == [date(2026, 8, 29)]
    assert [p.name for p in sorted(tmp_path.iterdir())] == ["ledger.json"]


def test_pruning_keeps_a_full_keep_days_window(tmp_path):
    ledger = PostsLedger(tmp_path / "ledger.json", keep_days=30).load()
    for offset in range(40):
        ledger.snapshot(date(2026, 7, 1) + timedelta(days=offset), [], {})
    assert len(ledger.days()) == 30
    assert ledger.days()[0] == date(2026, 7, 11)
    assert ledger.days()[-1] == date(2026, 8, 9)


def test_window_post_ids_boundary_is_inclusive_at_midnight(tmp_path):
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    on_cutoff = make_item(id="lw:on", published_at=datetime(2026, 8, 16, 0, 0, tzinfo=UTC))
    before_cutoff = make_item(
        id="lw:before", published_at=datetime(2026, 8, 15, 23, 59, tzinfo=UTC)
    )
    ledger.snapshot(date(2026, 8, 30), [on_cutoff, before_cutoff], {})
    assert ledger.window_post_ids(date(2026, 8, 30), days=14) == {"lw:on"}


def test_window_post_ids_handles_aware_published_at_without_crashing(tmp_path):
    """A tz-aware `published_at` from real data must not raise.

    The comparison drops the offset and reads the timestamp as written (deliberate simplicity),
    so a +13:00 post just past midnight counts as in-window even though its UTC instant is the
    afternoon before. Pinned here so a later timezone-correct rewrite is a visible change.
    """
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    kiritimati = timezone(timedelta(hours=13))
    late = make_item(id="lw:late", published_at=datetime(2026, 8, 16, 1, 0, tzinfo=kiritimati))
    naive = make_item(id="lw:naive", published_at=datetime(2026, 8, 20, 12, 0))
    ledger.snapshot(date(2026, 8, 30), [late, naive], {})

    assert ledger.window_post_ids(date(2026, 8, 30), days=14) == {"lw:late", "lw:naive"}


def test_window_post_ids_only_looks_at_that_day(tmp_path):
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    ledger.snapshot(date(2026, 8, 29), [make_item(id="lw:a")], {})
    assert ledger.window_post_ids(date(2026, 8, 31), days=14) == set()
    assert ledger.entries(date(2026, 8, 31)) == {}


def _boom(_src, _dst):
    """Stand-in for os.replace that fails the way a full disk would."""
    raise OSError("disk full")


# --- the carry-forward window -----------------------------------------------


def test_carry_forward_drops_a_post_published_outside_the_window(tmp_path):
    """The ledger is rewritten and pushed to the data repo every morning, so an unbounded
    carry-forward is a file that grows for ever. A post older than the 14-day window can no
    longer move the digest -- the window fetch does not even ask for it -- so carrying it costs
    bytes and buys nothing."""
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    day = date(2026, 8, 30)
    stale = make_item(id="lw:stale", published_at=datetime(2026, 8, 10, tzinfo=UTC))
    live = make_item(id="lw:live", published_at=datetime(2026, 8, 20, tzinfo=UTC))
    ledger.snapshot(day - timedelta(days=1), [stale, live], {})
    ledger.snapshot(day, [], {})

    assert set(ledger.entries(day)) == {"lw:live"}
    assert ledger.entries(day)["lw:live"].carried is True


def test_a_post_refetched_after_ageing_out_is_recorded_from_todays_fetch(tmp_path):
    """The filter is on the carry-forward alone: an item this run actually fetched is recorded
    whatever its age, so a `keep_days`-style pruning of live data can never happen by accident."""
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    day = date(2026, 8, 30)
    stale = make_item(id="lw:stale", published_at=datetime(2026, 8, 1, tzinfo=UTC), score=11)
    ledger.snapshot(day - timedelta(days=1), [stale], {})
    ledger.snapshot(day, [stale], {})

    entry = ledger.entries(day)["lw:stale"]
    assert entry.carried is False and entry.score == 11


def test_thirty_days_of_running_keeps_the_entry_count_flat(tmp_path):
    """The growth claim, simulated. Each day publishes a fixed number of posts and the fetch
    returns only those; everything else in the day's map is carried. Unbounded, day 30 held
    every post of all 30 days; bounded, it holds the window and stops there."""
    ledger = PostsLedger(tmp_path / "ledger.json").load()
    first = date(2026, 8, 1)
    per_day = 44
    for offset in range(30):
        day = first + timedelta(days=offset)
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
        fresh = [make_item(id=f"lw:{offset}-{n}", published_at=noon) for n in range(per_day)]
        ledger.snapshot(day, fresh, {})

    # Days D-14 to D inclusive: the cutoff is midnight, so the fifteenth day back still counts.
    assert len(ledger.entries(first + timedelta(days=29))) == 15 * per_day
    assert len(ledger.entries(first + timedelta(days=29))) == len(
        ledger.entries(first + timedelta(days=20))
    ), "flat, not growing"
    assert len(ledger.entries(first + timedelta(days=29))) < 30 * per_day

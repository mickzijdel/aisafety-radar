from datetime import UTC, date, datetime

from pipeline.floor import FloorConfig, apply_floor, filter_comments
from pipeline.models import Comment
from tests.test_models import make_item

TODAY = date(2026, 8, 30)
OLD_ACCOUNT = datetime(2020, 1, 1, tzinfo=UTC)
NEW_ACCOUNT = datetime(2026, 8, 20, tzinfo=UTC)


def test_karma_floor_per_site_with_curated_bypass():
    # frontpage=False matters: make_item defaults it to True, which is itself a bypass.
    low = make_item(id="lw:low", score=5, frontpage=False, author_created_at=OLD_ACCOUNT)
    curated_low = make_item(
        id="lw:cur", score=5, curated=True, frontpage=False, author_created_at=OLD_ACCOUNT
    )
    af_zero = make_item(
        id="af:z",
        site="af",
        source="alignmentforum",
        score=0,
        frontpage=False,
        author_created_at=OLD_ACCOUNT,
    )
    res = apply_floor([low, curated_low, af_zero], FloorConfig(), TODAY)
    assert {i.id for i in res.kept} == {"lw:cur", "af:z"}
    assert res.rejected[0].item_id == "lw:low" and "karma" in res.rejected[0].reason


def test_account_age_rejects_new_accounts_unless_frontpage():
    new = make_item(id="lw:new", score=50, frontpage=False, author_created_at=NEW_ACCOUNT)
    new_fp = make_item(id="lw:fp", score=50, frontpage=True, author_created_at=NEW_ACCOUNT)
    res = apply_floor([new, new_fp], FloorConfig(), TODAY)
    assert [i.id for i in res.kept] == ["lw:fp"]
    assert "account" in res.rejected[0].reason


def test_per_author_cap_keeps_highest():
    items = [
        make_item(id=f"lw:{n}", score=n, author="Prolific", author_created_at=OLD_ACCOUNT)
        for n in (30, 40, 50, 60)
    ]
    res = apply_floor(items, FloorConfig(), TODAY)
    assert sorted(i.id for i in res.kept) == ["lw:40", "lw:50", "lw:60"]


def test_anomaly_caps_to_median():
    items = [
        make_item(id=f"lw:{n}", score=100 - n, author=f"a{n}", author_created_at=OLD_ACCOUNT)
        for n in range(40)
    ]
    res = apply_floor(items, FloorConfig(), TODAY, source_medians={"lesswrong": 10})
    assert len(res.kept) == 10 and res.anomalies and "lesswrong" in res.anomalies[0]


def test_per_source_cap_keeps_highest_and_records_reason():
    items = [
        make_item(id=f"lw:{n}", score=n, author=f"a{n}", author_created_at=OLD_ACCOUNT)
        for n in (30, 40, 50)
    ]
    res = apply_floor(items, FloorConfig(per_source_per_day=2), TODAY)
    assert [i.id for i in res.kept] == ["lw:50", "lw:40"]
    assert [(r.item_id, r.reason) for r in res.rejected] == [("lw:30", "per-source cap")]


def test_equal_scores_break_ties_deterministically_across_input_orders():
    def build(order):
        return [
            make_item(id=f"lw:{n}", score=10, author=f"a{n}", author_created_at=OLD_ACCOUNT)
            for n in order
        ]

    cfg = FloorConfig(per_source_per_day=1)
    forward = apply_floor(build(["a", "b", "c"]), cfg, TODAY)
    reversed_ = apply_floor(build(["c", "b", "a"]), cfg, TODAY)
    assert [i.id for i in forward.kept] == [i.id for i in reversed_.kept] == ["lw:a"]


def test_unknown_account_age_is_treated_as_too_new():
    res = apply_floor(
        [make_item(id="lw:x", score=50, frontpage=False, author_created_at=None)],
        FloorConfig(),
        TODAY,
    )
    assert not res.kept
    assert "account" in res.rejected[0].reason


def test_anomaly_not_flagged_when_volume_is_normal():
    items = [
        make_item(id=f"lw:{n}", score=50, author=f"a{n}", author_created_at=OLD_ACCOUNT)
        for n in range(5)
    ]
    res = apply_floor(items, FloorConfig(), TODAY, source_medians={"lesswrong": 10})
    assert len(res.kept) == 5 and res.anomalies == []


def test_per_author_cap_is_per_source_not_global():
    items = [
        make_item(id=f"lw:{n}", score=50, author="Prolific", author_created_at=OLD_ACCOUNT)
        for n in range(3)
    ] + [
        make_item(
            id=f"eaf:{n}",
            site="eaf",
            source="eaforum",
            score=50,
            author="Prolific",
            author_created_at=OLD_ACCOUNT,
        )
        for n in range(3)
    ]
    res = apply_floor(items, FloorConfig(), TODAY)
    assert len(res.kept) == 6 and not res.rejected


def test_filter_comments():
    def c(cid, score, created):
        return Comment(
            id=cid,
            post_id="p",
            site="lw",
            author="x",
            author_created_at=created,
            posted_at=datetime(2026, 8, 30, tzinfo=UTC),
            score=score,
            agreement=None,
            parent_id=None,
            text="t",
        )

    kept = filter_comments(
        [c("ok", 5, OLD_ACCOUNT), c("low", 1, OLD_ACCOUNT), c("new", 9, NEW_ACCOUNT)],
        FloorConfig(),
        TODAY,
    )
    assert [x.id for x in kept] == ["ok"]

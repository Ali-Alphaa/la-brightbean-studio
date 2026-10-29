from apps.growth.services import viral


def samples(*likes):
    return [{"id": f"s{i}", "likes": n} for i, n in enumerate(likes)]


def test_viral_needs_50_likes_three_other_samples_and_3x_median():
    base = samples(10, 20, 30)  # median 20
    assert viral.decide(60, base, "new") == viral.VIRAL
    assert viral.decide(59, base, "new") == viral.NOT_VIRAL  # under 3x median
    assert viral.decide(49, samples(1, 1, 1), "new") == viral.NOT_VIRAL  # under 50 likes
    assert viral.ratio(60, base, "new") == 3.0


def test_fewer_than_three_other_samples_is_pending():
    assert viral.decide(500, samples(10, 10), "new") == viral.PENDING
    # The post's own sample doesn't count as "other".
    own = [*samples(10, 10), {"id": "new", "likes": 500}]
    assert viral.decide(500, own, "new") == viral.PENDING


def test_unknown_likes_are_never_judged_or_sampled():
    assert viral.decide(None, samples(1, 1, 1), "x") == viral.NOT_VIRAL
    assert viral.ratio(None, samples(1, 1, 1), "x") is None
    assert viral.add_sample(samples(5), "x", None) == samples(5)


def test_samples_keep_last_30_distinct_posts_and_refresh_counts():
    s = []
    for i in range(35):
        s = viral.add_sample(s, f"p{i}", i)
    assert len(s) == viral.MAX_SAMPLES
    assert s[0]["id"] == "p5"
    s = viral.add_sample(s, "p10", 999)
    assert [x for x in s if x["id"] == "p10"] == [{"id": "p10", "likes": 999}]
    assert len(s) == viral.MAX_SAMPLES


def test_pending_is_capped_and_resolved_as_samples_arrive():
    pending = []
    for i in range(12):
        pending = viral.add_pending(pending, {"id": f"c{i}", "likes": 100 + i})
    assert len(pending) == viral.MAX_PENDING
    assert pending[0]["id"] == "c2"

    now_viral, still = viral.resolve_pending([{"id": "c", "likes": 100}, {"id": "d", "likes": 60}], samples(10, 20))
    assert now_viral == [] and len(still) == 2
    now_viral, still = viral.resolve_pending([{"id": "c", "likes": 100}, {"id": "d", "likes": 60}], samples(10, 20, 30))
    assert [p["id"] for p in now_viral] == ["c", "d"] and still == []
    # Turned out ordinary: dropped from pending.
    now_viral, still = viral.resolve_pending([{"id": "e", "likes": 55}], samples(100, 100, 100))
    assert now_viral == [] and still == []

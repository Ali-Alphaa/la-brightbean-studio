import pytest

from apps.growth.services import analysis

from .conftest import at


@pytest.mark.django_db
def test_lift_is_relative_to_the_accounts_median(account, make_post):
    for likes in (10, 10, 10, 40):
        make_post(likes=likes)

    scored = sorted(analysis.score_posts(analysis.published_platform_posts([account])), key=lambda s: s.lift)

    assert scored[-1].lift == pytest.approx(4.0)
    assert scored[0].lift == pytest.approx(1.0)
    assert {s.basis for s in scored} == {"count"}


@pytest.mark.django_db
def test_rate_basis_rewards_the_post_with_fewer_views(workspace, make_post):
    from apps.social_accounts.models import SocialAccount

    threads = SocialAccount.objects.create(
        workspace=workspace,
        platform="threads",
        account_platform_id="t1",
        account_name="Threads",
        oauth_access_token="x",
    )
    small = make_post(likes=10, extra_stats={"views": 100}, social_account=threads)
    make_post(likes=10, extra_stats={"views": 1000}, social_account=threads)
    make_post(likes=10, extra_stats={"views": 1000}, social_account=threads)

    scored = {s.platform_post.id: s for s in analysis.score_posts(analysis.published_platform_posts([threads]))}

    assert scored[small.id].basis == "rate"
    assert scored[small.id].lift == pytest.approx(10.0)


@pytest.mark.django_db
def test_unmeasured_posts_are_skipped(account, make_post):
    from apps.composer.models import PlatformPost

    pp = make_post(likes=5)
    PlatformPost.objects.filter(id=pp.id).first().analytics_snapshots.all().delete()

    assert analysis.score_posts(analysis.published_platform_posts([account])) == []


@pytest.mark.django_db
def test_best_times_falls_back_to_defaults_with_thin_history(account, make_post):
    make_post(likes=50, published_at=at(5, 20))

    result = analysis.best_times(analysis.score_posts(analysis.published_platform_posts([account])), "UTC")

    assert result.source == "default"
    assert [(s.weekday, s.hour) for s in result.top] == analysis.DEFAULT_BEST_SLOTS
    assert result.grid[5][20] == (1, 1.0)


@pytest.mark.django_db
def test_best_times_ranks_the_slot_that_consistently_wins(account, make_post):
    for weeks in range(1, 5):
        make_post(likes=60, published_at=at(5, 20, weeks_ago=weeks))  # Saturday 20:00 wins
        make_post(likes=10, published_at=at(0, 8, weeks_ago=weeks))
        make_post(likes=10, published_at=at(2, 8, weeks_ago=weeks))

    result = analysis.best_times(analysis.score_posts(analysis.published_platform_posts([account])), "UTC", top_n=3)

    assert result.source == "history"
    assert (result.top[0].weekday, result.top[0].hour) == (5, 20)
    assert result.top[0].posts == 4
    assert result.top[0].lift > 3
    # Remaining recommendations are padded from defaults, never losing slots.
    assert all(slot.lift >= 1.0 for slot in result.top)


@pytest.mark.django_db
def test_best_times_uses_the_workspace_timezone(account, make_post):
    for weeks in range(1, 12):
        make_post(likes=10, published_at=at(1, 23, weeks_ago=weeks))

    result = analysis.best_times(analysis.score_posts(analysis.published_platform_posts([account])), "Asia/Tokyo")

    # Tue 23:00 UTC is Wed 08:00 in Tokyo.
    assert result.grid[2][8] is not None
    assert result.grid[1][23] is None


@pytest.mark.django_db
def test_apply_best_times_adds_and_optionally_replaces_slots(account):
    from apps.calendar.models import PostingSlot

    PostingSlot.objects.create(social_account=account, day_of_week=6, time="07:00")
    slots = [analysis.Slot(1, 9, 3, 2.0), analysis.Slot(3, 17, 2, 1.5)]

    assert analysis.apply_best_times(account, slots) == 2
    assert analysis.apply_best_times(account, slots) == 0
    assert PostingSlot.objects.filter(social_account=account, is_active=True).count() == 3

    analysis.apply_best_times(account, slots, replace=True)
    active = set(
        PostingSlot.objects.filter(social_account=account, is_active=True).values_list("day_of_week", flat=True)
    )
    assert active == {1, 3}


def test_extract_hashtags_dedupes_case_insensitively_and_ignores_anchors():
    text = "Launch day! #Launch #launch #Python3 see https://x.com/page#section ##double #123"
    assert analysis.extract_hashtags(text) == ["launch", "python3"]


@pytest.mark.django_db
def test_hashtag_stats_and_suggestions(account, make_post):
    for _ in range(3):
        make_post("New recipe for sourdough bread #baking #sourdough", likes=50)
        make_post("Morning coffee thoughts #coffee", likes=5)
        make_post("Weekend plans", likes=10)

    scored = analysis.score_posts(analysis.published_platform_posts([account]))
    stats = {h.tag: h for h in analysis.hashtag_stats(scored)}

    assert stats["sourdough"].uses == 3
    assert stats["sourdough"].lift > 1 > stats["coffee"].lift

    suggestions = [h.tag for h in analysis.suggest_hashtags_from_history("Fresh bread out of the oven #baking", scored)]
    assert suggestions == ["sourdough"]  # #baking already present, #coffee underperforms


@pytest.mark.django_db
def test_top_posts_keeps_each_accounts_top_share(account, make_post):
    likes = [100, 50, 10, 10, 10, 10, 10, 10, 10, 10]
    for value in likes:
        make_post(likes=value)

    best = analysis.top_posts(analysis.score_posts(analysis.published_platform_posts([account])), top_percent=20)

    assert [s.engagements for s in best] == [100, 50]

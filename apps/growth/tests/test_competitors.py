from unittest import mock

import httpx
import pytest

from apps.growth.services import competitors
from apps.growth.services.competitors import CompetitorError, normalize_handle


def test_normalize_handles():
    assert normalize_handle("bluesky", "@Alice.bsky.social") == "alice.bsky.social"
    assert normalize_handle("bluesky", "https://bsky.app/profile/alice.bsky.social/") == "alice.bsky.social"
    assert normalize_handle("mastodon", "@Gargron@Mastodon.Social") == "Gargron@mastodon.social"
    for platform, bad in [("bluesky", "not a handle"), ("mastodon", "gargron"), ("x", "@someone")]:
        with pytest.raises(CompetitorError):
            normalize_handle(platform, bad)


def _transport(routes):
    def handler(request: httpx.Request):
        for prefix, payload in routes.items():
            if str(request.url).startswith(prefix):
                status, body = payload
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


BSKY_PROFILE = {"handle": "alice.bsky.social", "displayName": "Alice", "followersCount": 1200, "postsCount": 300}
BSKY_FEED = {
    "feed": [
        {
            "post": {
                "uri": "at://did:plc:a/app.bsky.feed.post/3kabc",
                "record": {"text": "Big news", "createdAt": "2026-10-01T10:00:00.000Z"},
                "likeCount": 40,
                "replyCount": 5,
                "repostCount": 3,
                "quoteCount": 2,
            }
        },
        {"reason": {"$type": "app.bsky.feed.defs#reasonRepost"}, "post": {"uri": "at://x/y/z", "record": {}}},
    ]
}


def test_fetch_bluesky_parses_profile_and_skips_reposts():
    client = httpx.Client(
        transport=_transport(
            {
                f"{competitors.BLUESKY_API}/app.bsky.actor.getProfile": (200, BSKY_PROFILE),
                f"{competitors.BLUESKY_API}/app.bsky.feed.getAuthorFeed": (200, BSKY_FEED),
            }
        )
    )
    profile, posts = competitors.fetch_bluesky("alice.bsky.social", client)

    assert profile.followers == 1200
    assert len(posts) == 1
    assert posts[0].engagements == 50
    assert posts[0].url == "https://bsky.app/profile/alice.bsky.social/post/3kabc"


def test_fetch_mastodon_refuses_private_instances():
    with mock.patch.object(competitors, "is_safe_url", return_value=False), pytest.raises(CompetitorError):
        competitors.fetch_mastodon("bob@internal.example", httpx.Client(transport=_transport({})))


def test_fetch_mastodon_strips_html():
    client = httpx.Client(
        transport=_transport(
            {
                "https://social.example/api/v1/accounts/lookup": (
                    200,
                    {"id": "9", "display_name": "Bob", "followers_count": 10, "statuses_count": 2, "url": "u"},
                ),
                "https://social.example/api/v1/accounts/9/statuses": (
                    200,
                    [
                        {
                            "id": "1",
                            "url": "https://social.example/@bob/1",
                            "content": "<p>Hello &amp; welcome</p><p>Second   line</p>",
                            "created_at": "2026-10-02T08:00:00Z",
                            "favourites_count": 3,
                            "replies_count": 1,
                            "reblogs_count": 2,
                        }
                    ],
                ),
            }
        )
    )
    with mock.patch.object(competitors, "is_safe_url", return_value=True):
        profile, posts = competitors.fetch_mastodon("bob@social.example", client)

    assert profile.display_name == "Bob"
    assert posts[0].text == "Hello & welcome\n\nSecond line"
    assert posts[0].engagements == 6


@pytest.mark.django_db
def test_sync_records_snapshots_posts_and_errors(workspace):
    from apps.growth.models import Competitor

    competitor = Competitor.objects.create(workspace=workspace, platform="bluesky", handle="alice.bsky.social")
    profile = competitors.PublicProfile("alice.bsky.social", "Alice", "", "https://bsky.app/profile/a", 1200, 300)
    post = competitors.PublicPost("at://1", "u", "Big news", competitors._parse_dt("2026-10-01T10:00:00Z"), 40, 5, 5)

    with mock.patch.object(competitors, "fetch", return_value=(profile, [post])):
        competitors.sync_competitor(competitor)
        competitors.sync_competitor(competitor)  # same day: upserts, no duplicates

    competitor.refresh_from_db()
    assert competitor.followers == 1200
    assert competitor.snapshots.count() == 1
    assert competitor.posts.get().engagements == 50

    with mock.patch.object(competitors, "fetch", side_effect=CompetitorError("Account not found.")):
        competitors.sync_competitor(competitor)
    competitor.refresh_from_db()
    assert competitor.last_error == "Account not found."
    assert competitor.followers == 1200  # last good numbers are kept


@pytest.mark.django_db
def test_follower_delta_uses_dates_not_row_counts(workspace):
    from datetime import timedelta

    from django.utils import timezone

    from apps.growth.models import Competitor, CompetitorSnapshot

    competitor = Competitor.objects.create(workspace=workspace, platform="bluesky", handle="a.bsky.social")
    today = timezone.localdate()
    assert competitors.follower_delta(competitor, 7) is None
    for days_ago, followers in ((30, 900), (7, 1000), (0, 1100)):
        CompetitorSnapshot.objects.create(
            competitor=competitor, date=today - timedelta(days=days_ago), followers=followers
        )

    assert competitors.follower_delta(competitor, 7) == 100
    assert competitors.follower_delta(competitor, 30) == 200
    assert competitors.follower_delta(competitor, 90) is None

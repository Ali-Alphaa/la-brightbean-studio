from datetime import UTC, datetime
from unittest import mock

import httpx
import pytest

from apps.growth.models import AuthorBaseline, GrowthVoice, InspirationPost
from apps.growth.services import inspiration
from apps.growth.services.inspiration import InspirationError, parse_post_url

# --- URL validation (SSRF gate) ------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "platform", "external_id", "canonical"),
    [
        (
            "https://www.instagram.com/p/C1a2B3c4D5e/",
            "instagram",
            "C1a2B3c4D5e",
            "https://www.instagram.com/p/C1a2B3c4D5e/",
        ),
        (
            "https://instagram.com/reel/Cxyz_-123/?igsh=abc",
            "instagram",
            "Cxyz_-123",
            "https://www.instagram.com/p/Cxyz_-123/",
        ),
        ("http://www.instagram.com/someone/p/ABCDE12/", "instagram", "ABCDE12", "https://www.instagram.com/p/ABCDE12/"),
        (
            "https://www.tiktok.com/@Trader.Joe/video/7312345678901234567?lang=en",
            "tiktok",
            "7312345678901234567",
            "https://www.tiktok.com/@trader.joe/video/7312345678901234567",
        ),
    ],
)
def test_valid_post_urls(url, platform, external_id, canonical):
    link = parse_post_url(url)
    assert (link.platform, link.external_id, link.url) == (platform, external_id, canonical)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://www.instagram.com/p/ABCDE12/",
        "https://evil.com/p/ABCDE12/",
        "https://instagram.com.evil.com/p/ABCDE12/",
        "https://user:pw@www.instagram.com/p/ABCDE12/",
        "https://www.instagram.com:8443/p/ABCDE12/",
        "https://www.instagram.com/explore/",
        "https://www.instagram.com/p/../../admin/",
        "https://169.254.169.254/latest/meta-data/",
        "https://vm.tiktok.com/ZMabc/",
        "https://www.tiktok.com/@user",
        "https://www.tiktok.com/@user/video/notanumber",
        "https://www.tiktok.com/@user/video/123/../../x",
        "javascript:alert(1)",
    ],
)
def test_rejected_urls(url):
    with pytest.raises(InspirationError):
        parse_post_url(url)


# --- oEmbed parsing ------------------------------------------------------------------------


def test_parse_tiktok_oembed():
    data = {
        "title": "3 rules for risk management #trading",
        "author_name": "Trader Joe",
        "author_unique_id": "Trader.Joe",
        "author_url": "https://www.tiktok.com/@trader.joe",
        "embed_product_id": "7312345678901234567",
    }
    assert inspiration.parse_tiktok_oembed(data) == {
        "caption": "3 rules for risk management #trading",
        "author_name": "Trader Joe",
        "author_handle": "trader.joe",
        "external_id": "7312345678901234567",
        "media_type": "video",
    }
    # Falls back to the author URL; tolerates junk.
    assert inspiration.parse_tiktok_oembed({"author_url": "https://www.tiktok.com/@x_y"})["author_handle"] == "x_y"
    assert inspiration.parse_tiktok_oembed("nope") == {}


def test_parse_instagram_oembed():
    out = inspiration.parse_instagram_oembed({"author_name": "fxguy", "title": "Caption", "html": "<blockquote>"})
    assert out == {"caption": "Caption", "author_name": "fxguy", "author_handle": "fxguy"}
    assert inspiration.parse_instagram_oembed(None) == {}


@pytest.mark.django_db
def test_save_by_link_tiktok_uses_oembed_without_redirects(workspace):
    response = httpx.Response(200, json={"title": "Stop loss tips", "author_unique_id": "fx", "author_name": "FX"})
    with mock.patch("httpx.Client.get", return_value=response) as get:
        post, note = inspiration.save_by_link(workspace, "https://www.tiktok.com/@fx/video/7312345678901234567")
    assert note == ""
    assert (post.platform, post.external_id, post.caption, post.author_handle, post.source) == (
        "tiktok",
        "7312345678901234567",
        "Stop loss tips",
        "fx",
        "link",
    )
    assert get.call_args.args[0] == "https://www.tiktok.com/oembed"
    assert get.call_args.kwargs["params"] == {"url": "https://www.tiktok.com/@fx/video/7312345678901234567"}


@pytest.mark.django_db
def test_save_by_link_instagram_without_meta_app_needs_pasted_caption(workspace):
    with mock.patch.object(inspiration, "instagram_app_token", return_value=""):
        with pytest.raises(InspirationError):
            inspiration.save_by_link(workspace, "https://www.instagram.com/p/ABCDE12/")
        post, note = inspiration.save_by_link(
            workspace, "https://www.instagram.com/p/ABCDE12/", pasted_caption="My caption"
        )
    assert post.caption == "My caption" and "No Meta app" in note


# --- capture ----------------------------------------------------------------------------------


def item(id, likes, author="guru", **kw):
    raw = {"id": id, "authorHandle": author, "text": f"post {id}", "likes": likes, **kw}
    return inspiration.normalize_item("instagram", raw)


@pytest.mark.django_db
def test_capture_saves_only_viral_and_holds_pending(workspace):
    # Viral-looking post arrives before the baseline exists: pending.
    result = inspiration.capture(workspace, "instagram", [item("big", 300)])
    assert result.saved == 0
    assert InspirationPost.objects.count() == 0
    baseline = AuthorBaseline.objects.get(workspace=workspace, handle="guru")
    assert [p["id"] for p in baseline.pending] == ["big"]

    # Three ordinary posts arrive: the pending one is re-checked and saved.
    result = inspiration.capture(workspace, "instagram", [item("a", 40), item("b", 50), item("c", 60)])
    assert result.saved == 1
    post = InspirationPost.objects.get()
    assert (post.external_id, post.source, post.ratio) == ("big", "auto", 6.0)
    baseline.refresh_from_db()
    assert baseline.pending == []
    assert len(baseline.samples) == 4


@pytest.mark.django_db
def test_capture_null_likes_are_unknown_not_zero(workspace):
    items = [item("a", 10), item("b", 10), item("c", 10), item("unknown", None)]
    result = inspiration.capture(workspace, "instagram", items)
    assert result.saved == 0
    baseline = AuthorBaseline.objects.get(workspace=workspace, handle="guru")
    assert {s["id"] for s in baseline.samples} == {"a", "b", "c"}  # null likes isn't a 0 sample
    assert baseline.pending == []

    # A manual save with null counts is still saved, with null counts.
    result = inspiration.capture(workspace, "instagram", [item("m", None, manual=True, views=None, saves=None)])
    assert (result.saved, result.manual) == (1, 1)
    post = InspirationPost.objects.get(external_id="m")
    assert (post.likes, post.views, post.ratio, post.source) == (None, None, None, "manual")


@pytest.mark.django_db
def test_capture_own_posts_feed_voice_not_library(workspace):
    items = [
        item("o1", 900, author="me", postedAt="2026-10-01T10:00:00Z"),
        item("o2", 5, author="someone", own=True),
    ]
    result = inspiration.capture(workspace, "instagram", items, own_handle="@Me")
    assert result.saved == 0 and result.own == 2
    assert InspirationPost.objects.count() == 0
    voice = GrowthVoice.objects.get(workspace=workspace, platform="instagram", handle="me")
    assert {s["id"] for s in voice.samples} == {"o1", "o2"}
    assert not AuthorBaseline.objects.filter(handle="me").exists()


def test_start_of_local_day():
    now = datetime(2026, 10, 29, 23, 30, tzinfo=UTC)
    assert inspiration.start_of_local_day(0, now) == datetime(2026, 10, 29, 0, 0, tzinfo=UTC)
    # 60 minutes east of UTC: it's already the 30th locally.
    assert inspiration.start_of_local_day(60, now) == datetime(2026, 10, 29, 23, 0, tzinfo=UTC)
    assert inspiration.start_of_local_day(-300, now) == datetime(2026, 10, 29, 5, 0, tzinfo=UTC)


# --- Business Discovery ------------------------------------------------------------------------


def test_parse_discovery_ranks_by_ratio_vs_author_median():
    data = {
        "business_discovery": {
            "username": "FxBrand",
            "name": "FX Brand",
            "followers_count": 1000,
            "media": {
                "data": [
                    {
                        "id": "1",
                        "caption": "a",
                        "like_count": 10,
                        "media_type": "IMAGE",
                        "timestamp": "2026-10-01T00:00:00+0000",
                    },
                    {"id": "2", "caption": "b", "like_count": 20, "media_type": "CAROUSEL_ALBUM"},
                    {
                        "id": "3",
                        "caption": "c",
                        "like_count": 300,
                        "media_type": "VIDEO",
                        "permalink": "https://www.instagram.com/p/x/",
                    },
                    {"id": "4", "caption": "d"},
                ]
            },
        }
    }
    profile, items = inspiration.parse_discovery(data, "fxbrand")
    assert profile == {"username": "FxBrand", "name": "FX Brand", "followers": 1000}
    assert [i["external_id"] for i in items][:1] == ["3"]
    assert items[0]["ratio"] == 20.0  # 300 / median(10, 20)
    assert items[0]["media_type"] == "video" and items[0]["author_handle"] == "fxbrand"
    assert next(i for i in items if i["external_id"] == "4")["ratio"] is None
    assert next(i for i in items if i["external_id"] == "1")["posted_at"].year == 2026


@pytest.mark.django_db
def test_discover_requires_facebook_login_instagram_account(workspace):
    with pytest.raises(InspirationError, match="Connect an Instagram"):
        inspiration.discover_instagram(workspace, "@fxbrand")
    with pytest.raises(InspirationError, match="username"):
        inspiration.discover_instagram(workspace, "bad handle!")


# --- tagging -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_analyze_pending_tags_in_batches(workspace):
    from apps.growth.services import ai

    for i in range(25):
        InspirationPost.objects.create(workspace=workspace, platform="instagram", external_id=str(i), caption="x")
    calls = []

    def fake(posts):
        calls.append(len(posts))
        return [ai.PostTag(id=p["id"], topics=["forex"], hook_type="list", format="meme", on_niche=True) for p in posts]

    with mock.patch.object(ai, "tag_posts", side_effect=fake):
        assert inspiration.analyze_pending(workspace) == 25
    assert calls == [20, 5]
    assert not InspirationPost.objects.filter(analyzed_at__isnull=True).exists()
    assert InspirationPost.objects.filter(topics=["forex"], on_niche=True).count() == 25

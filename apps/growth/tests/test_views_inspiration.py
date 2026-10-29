from unittest import mock

import pytest

from apps.composer.models import Post
from apps.growth.models import CommentDMLog, CommentDMRule, GrowthVoice, InspirationPost, RepurposeRun
from apps.growth.services import ai
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount

from .test_views import _member, url


@pytest.fixture
def owner_client(client, workspace):
    return _member(client, workspace, WorkspaceMembership.WorkspaceRole.OWNER, "owner@example.com")


@pytest.fixture
def ig(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="1784",
        account_name="FX Me",
        account_handle="fxme",
        oauth_access_token="token",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def library(workspace):
    return [
        InspirationPost.objects.create(
            workspace=workspace,
            platform="instagram",
            external_id="a",
            caption="Risk 1% per trade",
            ratio=4.2,
            likes=420,
            topics=["risk"],
            hook_type="list",
            on_niche=True,
        ),
        InspirationPost.objects.create(
            workspace=workspace,
            platform="tiktok",
            external_id="b",
            caption="My cat",
            ratio=9.0,
            likes=900,
            on_niche=False,
        ),
        InspirationPost.objects.create(workspace=workspace, platform="instagram", external_id="c", caption="Untagged"),
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["inspiration", "voices", "comment_dm"])
def test_pages_render(owner_client, workspace, ig, library, name):
    response = owner_client.get(url(name, workspace))
    assert response.status_code == 200
    assert b"Inspiration" in response.content


@pytest.mark.django_db
def test_library_filters_and_ratio_badge(owner_client, workspace, library, settings):
    settings.ANTHROPIC_API_KEY = ""
    settings.GROWTH_AI_API_KEY = ""
    html = owner_client.get(url("inspiration", workspace)).content.decode()
    assert "4.2× usual" in html and "My cat" not in html and "Untagged" in html  # off-niche hidden by default
    assert "AI not configured" in html
    html = owner_client.get(url("inspiration", workspace), {"niche": "all", "platform": "tiktok"}).content.decode()
    assert "My cat" in html and "Risk 1%" not in html
    html = owner_client.get(url("inspiration", workspace), {"topic": "risk"}).content.decode()
    assert "Risk 1%" in html and "Untagged" not in html


@pytest.mark.django_db
def test_hide_toggles(owner_client, workspace, library):
    owner_client.post(url("inspiration_hide", workspace, post_id=library[0].id))
    library[0].refresh_from_db()
    assert library[0].hidden


@pytest.mark.django_db
def test_repurpose_flow_creates_draft_in_composer(owner_client, workspace, ig, library, settings):
    settings.ANTHROPIC_API_KEY = "sk-ant"
    five = [{"hook": f"Hook {i}", "caption": f"Hook {i}\n\nBody {i}", "script": []} for i in range(5)]
    with mock.patch.object(ai, "repurpose", return_value=five) as gen:
        response = owner_client.post(
            url("inspiration_repurpose", workspace), {"selected": [library[0].id], "platform": "instagram"}
        )
    run = RepurposeRun.objects.get()
    assert response.status_code == 302 and str(run.id) in response.url
    assert gen.call_args.kwargs["platform"] == "instagram"
    page = owner_client.get(response.url).content.decode()
    assert page.count('name="index"') == 5 and "/ 2200" in page

    response = owner_client.post(
        url("repurpose_choose", workspace, run_id=run.id),
        {"index": 2, "caption": "Edited caption", "account": ig.id, "action": "compose"},
    )
    post = Post.objects.get()
    assert response.status_code == 302 and str(post.id) in response.url and "/compose/" in response.url
    assert post.caption == "Edited caption"
    assert post.platform_posts.get().social_account == ig
    run.refresh_from_db()
    assert (run.chosen_index, run.post_id) == (2, post.id)


@pytest.mark.django_db
def test_repurpose_without_ai_or_selection(owner_client, workspace, library, settings):
    settings.ANTHROPIC_API_KEY = ""
    settings.GROWTH_AI_API_KEY = ""
    owner_client.post(url("inspiration_repurpose", workspace), {"selected": [library[0].id], "platform": "instagram"})
    settings.ANTHROPIC_API_KEY = "sk-ant"
    owner_client.post(url("inspiration_repurpose", workspace), {"selected": [], "platform": "instagram"})
    assert not RepurposeRun.objects.exists()


@pytest.mark.django_db
def test_tiktok_target_shows_audit_notice_and_privacy_options(owner_client, workspace):
    tiktok = SocialAccount.objects.create(
        workspace=workspace, platform="tiktok", account_platform_id="tt", account_name="TT", oauth_access_token="t"
    )
    run = RepurposeRun.objects.create(
        workspace=workspace,
        target_platform="tiktok",
        variations=[{"hook": "h", "caption": "c", "script": ["beat one"]}] * 5,
    )
    with mock.patch(
        "apps.growth.services.repurpose.tiktok_privacy", return_value={"options": ["SELF_ONLY"], "self_only": True}
    ):
        html = owner_client.get(url("repurpose_detail", workspace, run_id=run.id)).content.decode()
    assert "TikTok posts are private until your TikTok app passes TikTok" in html
    assert "SELF_ONLY" in html and "beat one" in html and tiktok.account_name in html


@pytest.mark.django_db
def test_tiktok_privacy_reads_creator_info(workspace):
    from apps.growth.services import repurpose

    account = SocialAccount.objects.create(
        workspace=workspace, platform="tiktok", account_platform_id="tt", account_name="TT"
    )
    provider = mock.MagicMock()
    provider.query_creator_info.return_value = {"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]}
    with mock.patch("apps.publisher.engine._provider_and_access_token", return_value=(provider, "t")):
        assert repurpose.tiktok_privacy(account) == {"options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"], "self_only": False}
    provider.query_creator_info.side_effect = RuntimeError
    with mock.patch("apps.publisher.engine._provider_and_access_token", return_value=(provider, "t")):
        assert repurpose.tiktok_privacy(account) == {"options": None, "self_only": None}


@pytest.mark.django_db
def test_voices_chip_per_account_save_and_regenerate(owner_client, workspace, ig, settings):
    owner_client.get(url("voices", workspace))
    voice = GrowthVoice.objects.get(workspace=workspace, platform="instagram", handle="fxme")
    assert voice.social_account == ig
    owner_client.post(url("voice_save", workspace, voice_id=voice.id), {"style_guide": "Short, punchy."})
    voice.refresh_from_db()
    assert voice.style_guide == "Short, punchy."

    settings.ANTHROPIC_API_KEY = "sk-ant"
    voice.samples = [{"id": "1", "text": "Cut losses early.", "likes": 50}]
    voice.save()
    with mock.patch.object(ai, "style_guide", return_value="- Direct\n- No hype") as gen:
        owner_client.post(url("voice_regenerate", workspace, voice_id=voice.id))
    voice.refresh_from_db()
    assert voice.style_guide == "- Direct\n- No hype" and voice.style_guide_generated_at
    assert gen.call_args.args[0] == [{"text": "Cut losses early.", "likes": 50, "comments": None}]


@pytest.mark.django_db
def test_pull_recent_instagram_posts(owner_client, workspace, ig):
    owner_client.get(url("voices", workspace))
    voice = GrowthVoice.objects.get()
    provider = mock.MagicMock()
    provider._request.return_value.json.return_value = {
        "data": [{"id": "m1", "caption": "Patience pays", "like_count": 80, "timestamp": "2026-10-01T00:00:00+0000"}]
    }
    with mock.patch("apps.publisher.engine._provider_and_access_token", return_value=(provider, "t")):
        owner_client.post(url("voice_pull", workspace, voice_id=voice.id))
    voice.refresh_from_db()
    assert voice.samples[0]["text"] == "Patience pays"
    assert provider._request.call_args.args[1].endswith("/1784/media")


@pytest.mark.django_db
def test_comment_dm_create_and_simulate(owner_client, workspace, ig):
    owner_client.post(
        url("comment_dm", workspace),
        {
            "account": ig.id,
            "keywords": "guide, pdf",
            "dm_text": "Link: https://x.y",
            "daily_cap": "50",
            "once_per_user": "on",
        },
    )
    rule = CommentDMRule.objects.get()
    assert (rule.keywords, rule.daily_cap, rule.once_per_user) == (["guide", "pdf"], 50, True)

    response = owner_client.post(
        url("comment_dm_simulate", workspace, rule_id=rule.id), {"text": "Send the GUIDE", "handle": "t"}, follow=True
    )
    html = response.content.decode()
    assert "nothing was sent" in html and "Link: https://x.y" in html
    assert CommentDMLog.objects.get().status == "simulated"

    owner_client.post(url("comment_dm_toggle", workspace, rule_id=rule.id))
    rule.refresh_from_db()
    assert not rule.enabled


@pytest.mark.django_db
def test_collect_from_handle_then_save_selected(owner_client, workspace, ig):
    items = [
        {
            "external_id": "m1",
            "url": "https://www.instagram.com/p/x/",
            "author_handle": "fxbrand",
            "author_name": "FX",
            "caption": "Big one",
            "likes": 900,
            "comments": 3,
            "media_type": "video",
            "posted_at": None,
            "ratio": 9.0,
        },
        {
            "external_id": "m2",
            "url": "",
            "author_handle": "fxbrand",
            "author_name": "FX",
            "caption": "Small one",
            "likes": 100,
            "comments": 1,
            "media_type": "image",
            "posted_at": None,
            "ratio": 1.0,
        },
    ]
    profile = {"username": "fxbrand", "name": "FX", "followers": 10}
    with mock.patch("apps.growth.services.inspiration.discover_instagram", return_value=(profile, items)):
        response = owner_client.post(url("inspiration_discover", workspace), {"handle": "@fxbrand"})
    html = response.content.decode()
    assert response.status_code == 200 and "Big one" in html and "9.0×" in html
    token = response.context["token"]
    with mock.patch("apps.growth.tasks.queue_tagging"):
        owner_client.post(url("inspiration_discover_save", workspace), {"token": token, "selected": ["m1"]})
    post = InspirationPost.objects.get()
    assert (post.external_id, post.source, post.ratio) == ("m1", "discovery", 9.0)

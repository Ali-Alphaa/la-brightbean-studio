from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
from django.utils import timezone

from apps.growth.models import AudienceContact, CommentDMLog, CommentDMRule
from apps.growth.services import comment_dm
from apps.social_accounts.models import SocialAccount


@pytest.fixture
def ig(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="1784",
        webhook_target_id="page1",
        account_name="FX Me",
        account_handle="fxme",
        oauth_access_token="token",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def rule(workspace, ig):
    return CommentDMRule.objects.create(
        workspace=workspace,
        social_account=ig,
        keywords=["guide", "free PDF"],
        dm_text="Here you go: https://example.com/guide",
        public_reply_text="Check your DMs!",
        tag="lead",
    )


@pytest.fixture(autouse=True)
def _no_queue():
    with mock.patch("apps.growth.tasks.send_comment_dm") as queued:
        yield queued


def test_parse_keywords():
    assert comment_dm.parse_keywords("Guide, guide,\n free PDF ,,") == ["Guide", "free PDF"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Send me the GUIDE please", "guide"),
        ("guide!", "guide"),
        ("I want the free pdf", "free PDF"),
        ("guidebook", None),
        ("misguided", None),
        ("free pdfs", None),
        ("", None),
    ],
)
def test_keyword_matching_is_case_insensitive_whole_word(text, expected):
    assert comment_dm.match_keyword(text, ["guide", "free PDF"]) == expected


def comment(cid, text="guide please", who="u1", **kw):
    return {"comment_id": cid, "text": text, "commenter_id": who, "commenter_handle": f"h{who}", "media_id": "m1", **kw}


@pytest.mark.django_db
def test_a_comment_is_answered_once(ig, rule, _no_queue):
    comment_dm.handle_instagram_comment(ig, **comment("c1"))
    comment_dm.handle_instagram_comment(ig, **comment("c1"))  # webhook redelivery / poll
    assert CommentDMLog.objects.filter(comment_id="c1").count() == 1
    _no_queue.assert_called_once()
    assert CommentDMLog.objects.get().status == "pending"


@pytest.mark.django_db
def test_no_match_or_wrong_post_or_disabled(ig, rule, workspace):
    assert comment_dm.record_match(ig, **comment("c1", text="nice post")) is None
    rule.media_id = "other-post"
    rule.save()
    assert comment_dm.record_match(ig, **comment("c2")) is None
    rule.media_id = ""
    rule.enabled = False
    rule.save()
    assert comment_dm.record_match(ig, **comment("c3")) is None
    facebook = SocialAccount.objects.create(
        workspace=workspace, platform="facebook", account_platform_id="p", account_name="f"
    )
    assert comment_dm.record_match(facebook, **comment("c4")) is None


@pytest.mark.django_db
def test_daily_cap_and_once_per_user(ig, rule):
    rule.daily_cap = 2
    rule.save()
    assert comment_dm.record_match(ig, **comment("c1", who="a")).status == "pending"
    again = comment_dm.record_match(ig, **comment("c2", who="a"))
    assert again.status == "skipped" and "Already sent" in again.error
    assert comment_dm.record_match(ig, **comment("c3", who="b")).status == "pending"
    capped = comment_dm.record_match(ig, **comment("c4", who="c"))
    assert capped.status == "skipped" and "Daily cap" in capped.error
    # Yesterday's sends no longer count.
    CommentDMLog.objects.filter(status="pending").update(created_at=timezone.now() - timedelta(days=2))
    assert comment_dm.record_match(ig, **comment("c5", who="d")).status == "pending"

    rule.once_per_user = False
    rule.daily_cap = 100
    rule.save()
    assert comment_dm.record_match(ig, **comment("c6", who="a")).status == "pending"


@pytest.mark.django_db
def test_seven_day_window_and_comments_older_than_the_rule(ig, rule):
    # Comments from before the rule existed (e.g. inbox backfill) are ignored entirely.
    assert comment_dm.record_match(ig, **comment("c0", commented_at=timezone.now() - timedelta(hours=1))) is None
    CommentDMRule.objects.filter(id=rule.id).update(created_at=timezone.now() - timedelta(days=30))
    old = comment_dm.record_match(ig, **comment("c1", commented_at=timezone.now() - timedelta(days=8)))
    assert old.status == "skipped" and "7-day" in old.error


@pytest.mark.django_db
def test_send_private_reply_public_reply_and_tag(ig, rule):
    log = comment_dm.record_match(ig, **comment("c1"))
    provider = mock.MagicMock()
    provider.send_private_reply.return_value = SimpleNamespace(platform_message_id="mid.1")
    provider.reply_to_comment.return_value = SimpleNamespace(platform_message_id="r1")
    with mock.patch("apps.publisher.engine._provider_and_access_token", return_value=(provider, "tok")):
        assert comment_dm.send(log) == "sent"
        assert comment_dm.send(log) == "sent"  # second run is a no-op
    provider.send_private_reply.assert_called_once_with("tok", "c1", rule.dm_text)
    provider.reply_to_comment.assert_called_once_with("tok", "c1", "Check your DMs!")
    log.refresh_from_db()
    assert (log.status, log.dm_message_id, log.public_reply_id) == ("sent", "mid.1", "r1")
    contact = AudienceContact.objects.get(platform="instagram", handle="u1")
    assert contact.tags == ["lead"] and contact.last_interaction_at is not None


@pytest.mark.django_db
def test_send_failure_is_logged_not_raised(ig, rule):
    log = comment_dm.record_match(ig, **comment("c1"))
    provider = mock.MagicMock()
    provider.send_private_reply.side_effect = RuntimeError("(#10) outside of allowed window")
    with mock.patch("apps.publisher.engine._provider_and_access_token", return_value=(provider, "tok")):
        assert comment_dm.send(log) == "failed"
    log.refresh_from_db()
    assert "allowed window" in log.error
    provider.reply_to_comment.assert_not_called()


@pytest.mark.django_db
def test_simulation_sends_nothing(ig, rule, _no_queue):
    log = comment_dm.record_match(ig, **comment("sim-1"), simulate=True)
    assert log.status == "simulated"
    _no_queue.assert_not_called()


@pytest.mark.django_db
def test_instagram_webhook_runs_rules_and_never_raises(ig, rule, _no_queue):
    from apps.inbox.webhooks import _upsert_instagram_comment

    value = {"id": "c9", "text": "guide pls", "from": {"id": "u9", "username": "trader9"}, "media": {"id": "m1"}}
    _upsert_instagram_comment(ig, value)
    log = CommentDMLog.objects.get(comment_id="c9")
    assert (log.commenter_id, log.commenter_handle, log.media_id) == ("u9", "trader9", "m1")
    _no_queue.assert_called_once()

    with mock.patch.object(comment_dm, "record_match", side_effect=RuntimeError("db down")):
        _upsert_instagram_comment(ig, {**value, "id": "c10"})  # must not raise


@pytest.mark.django_db
def test_instagram_provider_private_reply_payload():
    from providers.instagram import InstagramProvider
    from providers.instagram_login import InstagramLoginProvider

    response = mock.MagicMock()
    response.json.return_value = {"recipient_id": "u", "message_id": "mid.1"}
    fb = InstagramProvider({"page_id": "page1", "ig_user_id": "1784"})
    with mock.patch.object(fb, "_request", return_value=response) as req:
        assert fb.send_private_reply("tok", "c1", "hi").platform_message_id == "mid.1"
    assert req.call_args.args[1].endswith("/page1/messages")
    assert req.call_args.kwargs["json"] == {"recipient": {"comment_id": "c1"}, "message": {"text": "hi"}}

    ig_login = InstagramLoginProvider({"ig_user_id": "1784"})
    with mock.patch.object(ig_login, "_request", return_value=response) as req:
        ig_login.send_private_reply("tok", "c1", "hi")
    assert req.call_args.args[1] == "https://graph.instagram.com/v25.0/1784/messages"

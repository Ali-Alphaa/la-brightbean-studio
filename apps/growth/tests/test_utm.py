import pytest

from apps.growth.services.utm import add_utm, tag_for_platform_post, utm_params

PARAMS = {"utm_source": "bluesky", "utm_medium": "social", "utm_campaign": "launch"}


def test_adds_params_and_keeps_existing_query_and_fragment():
    text = "Read https://example.com/post?ref=1#top now"
    assert add_utm(text, PARAMS) == (
        "Read https://example.com/post?ref=1&utm_source=bluesky&utm_medium=social&utm_campaign=launch#top now"
    )


def test_never_overwrites_existing_utm_params_and_is_idempotent():
    text = "https://example.com/?utm_source=newsletter"
    once = add_utm(text, PARAMS)
    assert "utm_source=newsletter" in once
    assert "utm_source=bluesky" not in once
    assert add_utm(once, PARAMS) == once


def test_trailing_punctuation_and_brackets_stay_outside_the_link():
    text = "See (https://example.com/a). Also https://example.com/b!"
    tagged = add_utm(text, {"utm_medium": "social"})
    assert tagged == "See (https://example.com/a?utm_medium=social). Also https://example.com/b?utm_medium=social!"


def test_text_without_links_is_unchanged():
    assert add_utm("No links #here", PARAMS) == "No links #here"
    assert add_utm("", PARAMS) == ""


def test_platform_placeholder_and_blank_values():
    assert utm_params(source="{platform}", medium="social", campaign="", platform="threads") == {
        "utm_source": "threads",
        "utm_medium": "social",
    }


@pytest.mark.django_db
def test_tagging_is_off_until_the_workspace_enables_it(make_post):
    from apps.growth.models import GrowthSettings

    pp = make_post("x")
    assert tag_for_platform_post("https://example.com", pp) == "https://example.com"

    GrowthSettings.objects.create(workspace=pp.post.workspace, utm_enabled=True, utm_campaign="spring")
    assert (
        tag_for_platform_post("https://example.com", pp)
        == "https://example.com?utm_source=bluesky&utm_medium=social&utm_campaign=spring"
    )


@pytest.mark.django_db
def test_tagging_errors_never_block_publishing(make_post, monkeypatch):
    from apps.growth.models import GrowthSettings
    from apps.growth.services import utm

    pp = make_post("x")
    GrowthSettings.objects.create(workspace=pp.post.workspace, utm_enabled=True)
    monkeypatch.setattr(utm, "add_utm", lambda *a, **k: 1 / 0)
    assert tag_for_platform_post("https://example.com", pp) == "https://example.com"


@pytest.mark.django_db
def test_publish_engine_sends_tagged_caption_and_first_comment(make_post):
    from unittest.mock import MagicMock, patch

    from apps.growth.models import GrowthSettings
    from apps.publisher.engine import PublishEngine
    from providers.types import AuthType, PostType, PublishResult

    pp = make_post("Read https://example.com/post")
    pp.platform_specific_first_comment = "More: https://example.com/more"
    pp.save()
    GrowthSettings.objects.create(workspace=pp.post.workspace, utm_enabled=True, utm_campaign="launch")

    provider = MagicMock()
    provider.auth_type = AuthType.OAUTH2
    provider.supported_post_types = [PostType.TEXT]
    provider.publish_post.return_value = PublishResult(platform_post_id="p1", url="u", extra={})
    with (
        patch("apps.publisher.engine.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
    ):
        PublishEngine()._dispatch_to_provider(pp)

    _token, content = provider.publish_post.call_args.args
    assert content.text == "Read https://example.com/post?utm_source=bluesky&utm_medium=social&utm_campaign=launch"
    assert content.first_comment.endswith("/more?utm_source=bluesky&utm_medium=social&utm_campaign=launch")
    pp.post.refresh_from_db()
    assert pp.post.caption == "Read https://example.com/post"  # stored caption untouched

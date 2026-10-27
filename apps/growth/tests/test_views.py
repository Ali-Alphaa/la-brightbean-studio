from datetime import timedelta
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.calendar.models import PostingSlot
from apps.composer.models import Idea, PlatformPost
from apps.growth.models import (
    AudienceContact,
    AutoPlug,
    CaptionExperiment,
    Competitor,
    CompetitorPost,
    GrowthSettings,
    RecycledPost,
)
from apps.members.models import OrgMembership, WorkspaceMembership

from .conftest import at


def _member(client, workspace, role, email):
    user = User.objects.create_user(email=email, password="testpass123", tos_accepted_at=timezone.now())
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    client.force_login(user)
    return client


@pytest.fixture
def owner_client(client, workspace):
    return _member(client, workspace, WorkspaceMembership.WorkspaceRole.OWNER, "owner@example.com")


def url(name, workspace, **kwargs):
    return reverse(f"growth:{name}", kwargs={"workspace_id": workspace.id, **kwargs})


@pytest.fixture
def history(account, make_post):
    for weeks in range(1, 5):
        make_post("Sourdough tips #baking #bread", likes=60, published_at=at(5, 20, weeks_ago=weeks))
        make_post("Coffee thoughts #coffee", likes=10, published_at=at(0, 8, weeks_ago=weeks))
        make_post("Plain update", likes=10, published_at=at(2, 8, weeks_ago=weeks))


@pytest.mark.django_db
@pytest.mark.parametrize(
    "name", ["best_times", "hashtags", "top_posts", "competitors", "experiments", "plugs", "audience", "settings"]
)
def test_every_page_renders_for_an_owner(owner_client, workspace, history, name):
    response = owner_client.get(url(name, workspace))
    assert response.status_code == 200
    assert b"Growth" in response.content


@pytest.mark.django_db
def test_pages_render_without_any_accounts(owner_client, workspace):
    for name in ("best_times", "hashtags", "top_posts", "experiments", "plugs", "audience"):
        assert owner_client.get(url(name, workspace)).status_code == 200


@pytest.mark.django_db
def test_index_redirects_and_sidebar_links_growth(owner_client, workspace):
    response = owner_client.get(url("index", workspace), follow=True)
    assert response.redirect_chain[-1][0] == url("best_times", workspace)
    assert url("index", workspace).encode() in response.content


@pytest.mark.django_db
def test_best_times_shows_history_and_applies_slots(owner_client, workspace, account, history):
    response = owner_client.get(url("best_times", workspace))
    assert b"Sat 20:00" in response.content

    owner_client.post(url("apply_best_times", workspace), {"account": account.id, "count": 1})
    slot = PostingSlot.objects.get(social_account=account)
    assert (slot.day_of_week, slot.time.hour) == (5, 20)


@pytest.mark.django_db
def test_hashtag_suggestions_partial(owner_client, workspace, account, history):
    response = owner_client.post(
        url("hashtags", workspace),
        {"account": account.id, "caption": "Fresh bread today #bread"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert b"#baking" in response.content
    assert b"<nav" not in response.content  # partial only


@pytest.mark.django_db
def test_recycle_now_creates_a_draft_copy(owner_client, workspace, history):
    best = PlatformPost.objects.filter(post__caption__startswith="Sourdough").first()
    owner_client.post(url("recycle_now", workspace, post_id=best.post_id))
    copy = RecycledPost.objects.get(source_post=best.post).new_post
    assert copy.caption == best.post.caption
    assert copy.status == "draft"


@pytest.mark.django_db
def test_add_competitor_syncs_and_saves_inspiration_as_idea(owner_client, workspace):
    from apps.growth.services import competitors

    profile = competitors.PublicProfile("alice.bsky.social", "Alice", "", "https://bsky.app/profile/a", 10, 3)
    post = competitors.PublicPost("at://1", "https://bsky.app/x", "Great thread\nmore", timezone.now(), 9, 1, 1)
    with mock.patch.object(competitors, "fetch", return_value=(profile, [post])):
        owner_client.post(url("competitors", workspace), {"platform": "bluesky", "handle": "@alice.bsky.social"})
        owner_client.post(url("competitors", workspace), {"platform": "bluesky", "handle": "alice.bsky.social"})

    competitor = Competitor.objects.get(workspace=workspace)
    assert competitor.followers == 10
    page = owner_client.get(url("competitors", workspace))
    assert b"Great thread" in page.content

    inspiration = CompetitorPost.objects.get()
    owner_client.post(url("inspiration_to_idea", workspace, post_id=inspiration.id))
    idea = Idea.objects.get(workspace=workspace)
    assert idea.title == "Great thread"
    assert "alice.bsky.social" in idea.description


@pytest.mark.django_db
def test_invalid_competitor_handle_shows_error(owner_client, workspace):
    response = owner_client.post(url("competitors", workspace), {"platform": "mastodon", "handle": "nope"}, follow=True)
    assert b"Enter a Mastodon handle" in response.content
    assert not Competitor.objects.exists()


@pytest.mark.django_db
def test_create_experiment_from_the_form(owner_client, workspace, account):
    start = (timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    response = owner_client.post(
        url("experiments", workspace),
        {
            "action": "create",
            "name": "Hooks",
            "account": account.id,
            "caption": ["Question?", "Statement.", ""],
            "start_at": start,
            "spacing_hours": 24,
            "evaluate_after_hours": 48,
        },
    )
    experiment = CaptionExperiment.objects.get(workspace=workspace)
    assert response.status_code == 302
    assert experiment.variants.count() == 2
    assert owner_client.get(url("experiment_detail", workspace, experiment_id=experiment.id)).status_code == 200


@pytest.mark.django_db
def test_experiment_form_keeps_input_on_error(owner_client, workspace, account):
    response = owner_client.post(
        url("experiments", workspace),
        {
            "action": "create",
            "name": "Hooks",
            "account": account.id,
            "caption": ["Only one"],
            "start_at": "2020-01-01T09:00",
        },
    )
    assert response.status_code == 200
    assert b"Only one" in response.content
    assert not CaptionExperiment.objects.exists()


@pytest.mark.django_db
def test_add_plug_and_settings(owner_client, workspace, make_post):
    pp = make_post(published_at=timezone.now() - timedelta(hours=1))
    owner_client.post(
        url("plugs", workspace), {"platform_post": pp.id, "text": "Newsletter: https://x.y", "threshold": 5}
    )
    assert AutoPlug.objects.get(platform_post=pp).threshold == 5

    owner_client.post(
        url("settings", workspace),
        {
            "utm_enabled": "on",
            "utm_source": "{platform}",
            "utm_medium": "social",
            "autoplug_enabled": "on",
            "autoplug_text": "Sub!",
            "autoplug_threshold": "7",
            "recycle_per_week": "99",
        },
    )
    settings = GrowthSettings.objects.get(workspace=workspace)
    assert settings.utm_enabled and settings.autoplug_enabled
    assert settings.autoplug_enabled_at is not None
    assert settings.recycle_per_week == 7  # clamped


@pytest.mark.django_db
def test_audience_lists_engagers_and_saves_notes(owner_client, workspace, account):
    from apps.inbox.models import InboxMessage

    for i in range(3):
        InboxMessage.objects.create(
            workspace=workspace,
            social_account=account,
            platform_message_id=f"m{i}",
            message_type="comment",
            sender_name="Fan",
            sender_handle="fan.bsky.social",
            body="love it",
            received_at=timezone.now(),
        )
    page = owner_client.get(url("audience", workspace))
    assert b"fan.bsky.social" in page.content

    owner_client.post(
        url("audience", workspace), {"platform": "bluesky", "handle": "fan.bsky.social", "tags": "superfan, lead"}
    )
    assert AudienceContact.objects.get().tags == ["superfan", "lead"]
    filtered = owner_client.get(url("audience", workspace) + "?tag=superfan")
    assert b"fan.bsky.social" in filtered.content


@pytest.mark.django_db
def test_viewer_can_look_but_not_change_settings_or_create(client, workspace, account):
    viewer = _member(client, workspace, WorkspaceMembership.WorkspaceRole.VIEWER, "viewer@example.com")
    assert viewer.get(url("settings", workspace)).status_code == 403
    assert viewer.post(url("apply_best_times", workspace), {"account": account.id}).status_code == 403
    viewer.post(url("competitors", workspace), {"platform": "bluesky", "handle": "alice.bsky.social"})
    assert not Competitor.objects.exists()


@pytest.mark.django_db
def test_other_workspaces_objects_are_not_reachable(owner_client, workspace, organization):
    from apps.workspaces.models import Workspace

    other = Workspace.objects.create(name="Other", organization=organization)
    competitor = Competitor.objects.create(workspace=other, platform="bluesky", handle="x.bsky.social")
    response = owner_client.post(url("competitor_delete", workspace, competitor_id=competitor.id))
    assert response.status_code == 404
    assert Competitor.objects.filter(id=competitor.id).exists()

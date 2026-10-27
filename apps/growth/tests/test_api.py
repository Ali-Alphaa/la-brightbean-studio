import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.growth.models import Competitor, CompetitorPost, GrowthSettings
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount

from .conftest import at


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def key_for(user, workspace):
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )

    def _issue(accounts, permissions=None):
        key = services.issue_api_key(
            workspace=workspace,
            social_accounts=accounts,
            issued_by=user,
            name="growth",
            permissions=list(permissions if permissions is not None else PERMISSION_KEYS),
        )
        return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")

    return _issue


@pytest.fixture
def history(make_post):
    for weeks in range(1, 5):
        make_post("Bread #baking", likes=60, published_at=at(5, 20, weeks_ago=weeks))
        make_post("Coffee #coffee", likes=10, published_at=at(0, 8, weeks_ago=weeks))
        make_post("Update", likes=10, published_at=at(2, 8, weeks_ago=weeks))


@pytest.mark.django_db
def test_best_times_hashtags_and_top_posts(key_for, account, history):
    client = key_for([account])

    times = client.get(f"/api/v1/growth/accounts/{account.id}/best-times?top=1").json()
    assert times["source"] == "history"
    assert times["slots"][0] | {"lift": 0} == {"weekday": 5, "weekday_name": "Sat", "hour": 20, "posts": 4, "lift": 0}

    tags = client.get(f"/api/v1/growth/accounts/{account.id}/hashtags", {"caption": "New loaf"}).json()
    assert tags["ranked"][0]["tag"] == "baking"
    assert [h["tag"] for h in tags["suggested"]] == ["baking"]

    top = client.get("/api/v1/growth/top-posts?limit=3").json()
    assert {p["caption"] for p in top} == {"Bread #baking"}


@pytest.mark.django_db
def test_allowlist_and_permission_are_enforced(key_for, workspace, account, history):
    other = SocialAccount.objects.create(
        workspace=workspace, platform="threads", account_platform_id="t", account_name="T", oauth_access_token="x"
    )
    scoped = key_for([other])
    assert scoped.get(f"/api/v1/growth/accounts/{account.id}/best-times").status_code == 403
    assert scoped.get("/api/v1/growth/top-posts").json() == []  # only allowlisted accounts

    no_analytics = key_for([account], [p for p in PERMISSION_KEYS if p != "view_analytics"])
    assert no_analytics.get("/api/v1/growth/competitors").status_code == 403


@pytest.mark.django_db
def test_competitors_and_utm(key_for, workspace, account):
    competitor = Competitor.objects.create(workspace=workspace, platform="bluesky", handle="a.bsky.social", followers=5)
    CompetitorPost.objects.create(
        competitor=competitor, external_id="1", text="hi", published_at=timezone.now(), engagements=3
    )
    GrowthSettings.objects.create(workspace=workspace, utm_campaign="fall")
    client = key_for([account])

    data = client.get("/api/v1/growth/competitors").json()
    assert data[0]["handle"] == "a.bsky.social"
    assert data[0]["top_posts"][0]["text"] == "hi"

    tagged = client.post(
        "/api/v1/growth/utm", {"text": "Go https://ex.com", "platform": "linkedin"}, content_type="application/json"
    ).json()
    assert tagged["text"] == "Go https://ex.com?utm_source=linkedin&utm_medium=social&utm_campaign=fall"

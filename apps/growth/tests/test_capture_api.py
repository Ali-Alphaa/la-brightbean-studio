"""``/api/v1/growth/inspiration/*`` — the contract shared with the browser extension."""

from unittest import mock

import pytest
from django.test import Client

from apps.api_keys import services
from apps.growth.models import AuthorBaseline, GrowthVoice, InspirationPost
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

CAPTURE = "/api/v1/growth/inspiration/capture"
PING = "/api/v1/growth/inspiration/ping"


@pytest.fixture(autouse=True)
def _no_tagging_queue():
    with mock.patch("apps.growth.tasks.queue_tagging") as queued:
        yield queued


def _client(token=None, secure=True):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
    return Client(**headers), {"secure": secure}


@pytest.fixture
def issue(user):
    def _issue(workspace, permissions=None):
        OrgMembership.objects.get_or_create(
            user=user, organization=workspace.organization, defaults={"org_role": OrgMembership.OrgRole.OWNER}
        )
        WorkspaceMembership.objects.get_or_create(
            user=user, workspace=workspace, defaults={"workspace_role": WorkspaceMembership.WorkspaceRole.OWNER}
        )
        # Keys must allowlist at least one connected account; capture ignores the allowlist.
        account, _ = SocialAccount.objects.get_or_create(
            workspace=workspace,
            platform="bluesky",
            account_platform_id=f"did:{workspace.id}",
            defaults={"account_name": "x"},
        )
        key = services.issue_api_key(
            workspace=workspace,
            social_accounts=[account],
            issued_by=user,
            name="extension",
            permissions=list(PERMISSION_KEYS if permissions is None else permissions),
        )
        return key.plaintext_token

    return _issue


def post(token, body, secure=True):
    client, kw = _client(token, secure)
    return client.post(CAPTURE, body, content_type="application/json", **kw)


def items(*specs, author="guru"):
    return [
        {
            "id": spec[0],
            "url": f"https://www.instagram.com/p/{spec[0]}/",
            "authorHandle": author,
            "authorName": "Guru",
            "text": f"caption {spec[0]}",
            "likes": spec[1],
            "comments": None,
            "shares": None,
            "views": None,
            "saves": None,
            "mediaType": "image",
            "postedAt": None,
            "own": False,
            "manual": False,
            **(spec[2] if len(spec) > 2 else {}),
        }
        for spec in specs
    ]


@pytest.mark.django_db
def test_ping_returns_workspace_name_with_the_documented_header(workspace, issue):
    client, kw = _client(issue(workspace))
    response = client.get(PING, **kw)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "workspace": "Growth WS"}


@pytest.mark.django_db
def test_auth_is_required(workspace, issue):
    client, kw = _client()
    assert client.get(PING, **kw).status_code == 401
    assert post("bb_studio_notarealkey_xxx", {"platform": "instagram", "items": []}).status_code == 401
    # Plain HTTP is refused (behind the proxy, X-Forwarded-Proto: https makes it secure).
    assert post(issue(workspace), {"platform": "instagram", "items": []}, secure=False).status_code == 401


@pytest.mark.django_db
def test_capture_contract_and_counts(workspace, issue, _no_tagging_queue):
    token = issue(workspace)
    body = {
        "platform": "instagram",
        "ownHandle": "me",
        "utcOffset": -60,
        "items": items(("a", 40), ("b", 50), ("c", 60), ("viral", 600), ("saved", None, {"manual": True})),
    }
    response = post(token, body)
    assert response.status_code == 200, response.content
    assert response.json() == {"saved": 2, "manual": 1, "savedToday": 2}
    assert set(InspirationPost.objects.values_list("external_id", "source")) == {("viral", "auto"), ("saved", "manual")}
    _no_tagging_queue.assert_called_once()

    # Same batch again: rows are refreshed, not duplicated.
    assert post(token, body).json() == {"saved": 2, "manual": 1, "savedToday": 2}
    assert InspirationPost.objects.count() == 2


@pytest.mark.django_db
def test_null_counts_accepted_and_null_likes_not_sampled(workspace, issue):
    response = post(issue(workspace), {"platform": "tiktok", "items": items(("n1", None), ("n2", None), author="x")})
    assert response.status_code == 200
    assert response.json()["saved"] == 0
    assert AuthorBaseline.objects.get(handle="x").samples == []


@pytest.mark.django_db
def test_own_items_go_to_voice(workspace, issue):
    body = {"platform": "instagram", "ownHandle": "@Me", "items": items(("o1", 999, {"own": True}), author="me")}
    assert post(issue(workspace), body).json() == {"saved": 0, "manual": 0, "savedToday": 0}
    assert not InspirationPost.objects.exists()
    assert GrowthVoice.objects.get(workspace=workspace, handle="me").samples[0]["id"] == "o1"


@pytest.mark.django_db
def test_capture_is_scoped_to_the_keys_workspace(workspace, issue):
    other = Workspace.objects.create(name="Other", organization=workspace.organization)
    body = {"platform": "instagram", "items": items(("m1", 5, {"manual": True}))}
    assert post(issue(other), body).json()["saved"] == 1
    assert post(issue(workspace), {"platform": "instagram", "items": []}).json() == {
        "saved": 0,
        "manual": 0,
        "savedToday": 0,
    }
    assert InspirationPost.objects.get().workspace == other


@pytest.mark.django_db
def test_validation_and_permission(workspace, issue):
    token = issue(workspace)
    assert post(token, {"platform": "x", "items": []}).status_code == 422
    assert post(token, {"platform": "instagram", "items": items(*[(str(i), 1) for i in range(101)])}).status_code == 422
    assert post(token, {"platform": "instagram", "items": items(("a", -1))}).status_code == 422
    long_id = items(("a", 1))
    long_id[0]["id"] = "x" * 300
    assert post(token, {"platform": "instagram", "items": long_id}).status_code == 422
    bad_media = items(("a", 1))
    bad_media[0]["mediaType"] = "story"
    assert post(token, {"platform": "instagram", "items": bad_media}).status_code == 422

    no_perm = issue(workspace, [p for p in PERMISSION_KEYS if p != "view_analytics"])
    assert post(no_perm, {"platform": "instagram", "items": []}).status_code == 403
    client, kw = _client(no_perm)
    assert client.get(PING, **kw).status_code == 200  # ping needs only a valid key

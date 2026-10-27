"""Read-only tracking of public accounts on networks with open public APIs.

Bluesky (public AppView) and Mastodon (per-instance REST API) expose profiles
and recent posts without authentication, so tracking needs no credentials.
Networks that require an approved app per tracked account (Instagram, X,
LinkedIn, TikTok) aren't offered.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.common.validators import is_safe_url

logger = logging.getLogger(__name__)

BLUESKY_API = "https://public.api.bsky.app/xrpc"
TIMEOUT_SECONDS = 20
POSTS_PER_SYNC = 40
#: How long a competitor's numbers are considered fresh.
SYNC_INTERVAL_HOURS = 12

_TAG_RE = re.compile(r"<[^>]+>")
_BLUESKY_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,252}\.[a-z]{2,}$|^did:plc:[a-z0-9]+$")
_MASTODON_HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_]{1,30})@([A-Za-z0-9.-]+\.[A-Za-z]{2,})$")


class CompetitorError(Exception):
    """User-facing failure (bad handle, account not found, network down)."""


@dataclass
class PublicProfile:
    handle: str
    display_name: str
    avatar_url: str
    profile_url: str
    followers: int
    posts_count: int


@dataclass
class PublicPost:
    external_id: str
    url: str
    text: str
    published_at: datetime
    likes: int
    replies: int
    reposts: int

    @property
    def engagements(self) -> int:
        return self.likes + self.replies + self.reposts


def normalize_handle(platform: str, raw: str) -> str:
    handle = (raw or "").strip()
    if platform == "bluesky":
        handle = handle.lstrip("@").lower()
        handle = re.sub(r"^https?://bsky\.app/profile/", "", handle).strip("/")
        if not _BLUESKY_HANDLE_RE.match(handle):
            raise CompetitorError("Enter a Bluesky handle like name.bsky.social.")
        return handle
    if platform == "mastodon":
        match = _MASTODON_HANDLE_RE.match(handle)
        if not match:
            raise CompetitorError("Enter a Mastodon handle like @name@mastodon.social.")
        return f"{match.group(1)}@{match.group(2).lower()}"
    raise CompetitorError("That network isn't supported for tracking.")


def _get_json(client: httpx.Client, url: str, params: dict | None = None):
    try:
        response = client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise CompetitorError("Couldn't reach the network. Try again later.") from exc
    if response.status_code in (400, 404):
        raise CompetitorError("Account not found.")
    if response.status_code == 429:
        raise CompetitorError("The network is rate limiting requests. Try again later.")
    if response.status_code >= 400:
        raise CompetitorError(f"The network returned an error ({response.status_code}).")
    try:
        return response.json()
    except ValueError as exc:
        raise CompetitorError("The network returned an unexpected response.") from exc


def _parse_dt(value) -> datetime | None:
    parsed = parse_datetime(str(value or ""))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, UTC)
    return parsed


def _int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def fetch_bluesky(handle: str, client: httpx.Client) -> tuple[PublicProfile, list[PublicPost]]:
    profile = _get_json(client, f"{BLUESKY_API}/app.bsky.actor.getProfile", {"actor": handle})
    actor = profile.get("handle") or handle
    feed = _get_json(
        client,
        f"{BLUESKY_API}/app.bsky.feed.getAuthorFeed",
        {"actor": handle, "limit": POSTS_PER_SYNC, "filter": "posts_no_replies"},
    )
    posts: list[PublicPost] = []
    for item in feed.get("feed") or []:
        if item.get("reason"):  # a repost of someone else's post
            continue
        post = item.get("post") or {}
        record = post.get("record") or {}
        uri = post.get("uri") or ""
        published = _parse_dt(record.get("createdAt") or post.get("indexedAt"))
        if not uri or published is None:
            continue
        posts.append(
            PublicPost(
                external_id=uri,
                url=f"https://bsky.app/profile/{actor}/post/{uri.rsplit('/', 1)[-1]}",
                text=str(record.get("text") or ""),
                published_at=published,
                likes=_int(post.get("likeCount")),
                replies=_int(post.get("replyCount")),
                reposts=_int(post.get("repostCount")) + _int(post.get("quoteCount")),
            )
        )
    return (
        PublicProfile(
            handle=actor,
            display_name=str(profile.get("displayName") or actor),
            avatar_url=str(profile.get("avatar") or ""),
            profile_url=f"https://bsky.app/profile/{actor}",
            followers=_int(profile.get("followersCount")),
            posts_count=_int(profile.get("postsCount")),
        ),
        posts,
    )


def fetch_mastodon(handle: str, client: httpx.Client) -> tuple[PublicProfile, list[PublicPost]]:
    username, instance = handle.split("@", 1)
    base = f"https://{instance}"
    # The instance is user-supplied: refuse anything resolving to a private
    # address, and the client never follows redirects.
    if not is_safe_url(base):
        raise CompetitorError("That Mastodon server can't be reached.")
    account = _get_json(client, f"{base}/api/v1/accounts/lookup", {"acct": username})
    account_id = account.get("id")
    if not account_id:
        raise CompetitorError("Account not found.")
    statuses = _get_json(
        client,
        f"{base}/api/v1/accounts/{account_id}/statuses",
        {"limit": POSTS_PER_SYNC, "exclude_replies": "true", "exclude_reblogs": "true"},
    )
    posts: list[PublicPost] = []
    for status in statuses if isinstance(statuses, list) else []:
        published = _parse_dt(status.get("created_at"))
        if not status.get("id") or published is None:
            continue
        text = html.unescape(_TAG_RE.sub(" ", str(status.get("content") or "").replace("</p><p>", "\n\n")))
        posts.append(
            PublicPost(
                external_id=str(status["id"]),
                url=str(status.get("url") or ""),
                text=re.sub(r"[ \t]+", " ", text).strip(),
                published_at=published,
                likes=_int(status.get("favourites_count")),
                replies=_int(status.get("replies_count")),
                reposts=_int(status.get("reblogs_count")),
            )
        )
    return (
        PublicProfile(
            handle=handle,
            display_name=str(account.get("display_name") or username),
            avatar_url=str(account.get("avatar") or ""),
            profile_url=str(account.get("url") or f"{base}/@{username}"),
            followers=_int(account.get("followers_count")),
            posts_count=_int(account.get("statuses_count")),
        ),
        posts,
    )


FETCHERS = {"bluesky": fetch_bluesky, "mastodon": fetch_mastodon}


def fetch(platform: str, handle: str) -> tuple[PublicProfile, list[PublicPost]]:
    with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
        return FETCHERS[platform](handle, client)


def sync_competitor(competitor) -> None:
    """Refresh one competitor; records the error on the row instead of raising."""
    from apps.growth.models import CompetitorPost, CompetitorSnapshot

    try:
        profile, posts = fetch(competitor.platform, competitor.handle)
    except CompetitorError as exc:
        competitor.last_error = str(exc)
        competitor.last_synced_at = timezone.now()
        competitor.save(update_fields=["last_error", "last_synced_at"])
        return

    now = timezone.now()
    with transaction.atomic():
        competitor.display_name = profile.display_name[:255]
        competitor.avatar_url = profile.avatar_url[:500]
        competitor.profile_url = profile.profile_url[:500]
        competitor.followers = profile.followers
        competitor.posts_count = profile.posts_count
        competitor.last_synced_at = now
        competitor.last_error = ""
        competitor.save()
        CompetitorSnapshot.objects.update_or_create(
            competitor=competitor,
            date=timezone.localdate(now),
            defaults={"followers": profile.followers, "posts_count": profile.posts_count},
        )
        for post in posts:
            CompetitorPost.objects.update_or_create(
                competitor=competitor,
                external_id=post.external_id[:255],
                defaults={
                    "url": post.url[:500],
                    "text": post.text,
                    "published_at": post.published_at,
                    "likes": post.likes,
                    "replies": post.replies,
                    "reposts": post.reposts,
                    "engagements": post.engagements,
                },
            )


def follower_delta(competitor, days: int) -> int | None:
    """Follower change since the newest snapshot at least ``days`` days old.

    ``None`` until tracking has run that long, so a 7-day figure never
    silently reports a 2-day change.
    """
    latest = competitor.snapshots.order_by("-date").first()
    if latest is None:
        return None
    baseline = competitor.snapshots.filter(date__lte=latest.date - timedelta(days=days)).order_by("-date").first()
    if baseline is None:
        return None
    return latest.followers - baseline.followers

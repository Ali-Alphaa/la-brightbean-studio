"""``/api/v1/growth/*`` — growth insights for agents and automation tools (n8n, Make, …).

Read-only apart from ``/utm``, which only transforms the text it is given, and
``/inspiration/capture``, which the BrightBean browser extension uses to feed
the Instagram/TikTok inspiration library and the user's voice. Every route
except ``/inspiration/ping`` requires ``view_analytics``; account-specific
routes respect the key's account allowlist, exactly like ``/api/v1/analytics``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from ninja import Field, Query, Router, Schema

from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.api.routers.posts import _require_perm, _resolve_account
from apps.growth.models import Competitor, CompetitorPost, GrowthSettings
from apps.growth.services import analysis
from apps.growth.services.utm import add_utm, utm_params

router = Router(tags=["growth"])


class SlotOut(Schema):
    weekday: int
    weekday_name: str
    hour: int
    posts: int
    lift: float


class BestTimesResponse(Schema):
    account_id: uuid.UUID
    timezone: str
    source: str
    scored_posts: int
    slots: list[SlotOut]


class HashtagOut(Schema):
    tag: str
    uses: int
    lift: float


class HashtagsResponse(Schema):
    account_id: uuid.UUID
    ranked: list[HashtagOut]
    suggested: list[HashtagOut]


class TopPostOut(Schema):
    post_id: uuid.UUID
    account_id: uuid.UUID
    platform: str
    caption: str
    published_at: datetime
    engagements: float
    lift: float


class CompetitorPostOut(Schema):
    url: str
    text: str
    published_at: datetime
    likes: int
    replies: int
    reposts: int


class CompetitorOut(Schema):
    platform: str
    handle: str
    display_name: str
    followers: int
    posts_count: int
    last_synced_at: datetime | None
    top_posts: list[CompetitorPostOut]


class UtmIn(Schema):
    text: str
    platform: str = ""


class UtmOut(Schema):
    text: str


def _scored(accounts, days=analysis.DEFAULT_LOOKBACK_DAYS):
    return analysis.score_posts(analysis.published_platform_posts(accounts, days=days))


@router.get("/accounts/{account_id}/best-times", response=BestTimesResponse, summary="Best times to post")
def best_times(request, account_id: uuid.UUID, top: int = Query(7, ge=1, le=20)):
    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "view_analytics")
    account = _resolve_account(request, account_id)
    tz = request.workspace.effective_timezone or "UTC"
    result = analysis.best_times(_scored([account]), tz, top_n=top)
    log_audit_entry(request, action="growth.read.best_times", target_id=account.id, status_code=200)
    return {
        "account_id": account.id,
        "timezone": tz,
        "source": result.source,
        "scored_posts": result.scored_posts,
        "slots": [
            {
                "weekday": s.weekday,
                "weekday_name": analysis.DAY_NAMES[s.weekday],
                "hour": s.hour,
                "posts": s.posts,
                "lift": s.lift,
            }
            for s in result.top
        ],
    }


@router.get("/accounts/{account_id}/hashtags", response=HashtagsResponse, summary="Hashtag performance and suggestions")
def hashtags(request, account_id: uuid.UUID, caption: str = Query("", max_length=5000)):
    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "view_analytics")
    account = _resolve_account(request, account_id)
    scored = _scored([account])
    log_audit_entry(request, action="growth.read.hashtags", target_id=account.id, status_code=200)
    return {
        "account_id": account.id,
        "ranked": [h.__dict__ for h in analysis.hashtag_stats(scored, min_uses=2)[:50]],
        "suggested": [h.__dict__ for h in analysis.suggest_hashtags_from_history(caption, scored)] if caption else [],
    }


@router.get("/top-posts", response=list[TopPostOut], summary="Best-performing posts across allowed accounts")
def top_posts(request, top_percent: int = Query(20, ge=1, le=100), limit: int = Query(20, ge=1, le=100)):
    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "view_analytics")
    accounts = list(request.api_key.social_accounts.all())  # type: ignore[attr-defined]
    best = analysis.top_posts(_scored(accounts, days=365), top_percent=top_percent, limit=limit)
    log_audit_entry(request, action="growth.read.top_posts", target_id=None, status_code=200)
    return [
        {
            "post_id": s.platform_post.post_id,
            "account_id": s.platform_post.social_account_id,
            "platform": s.platform_post.social_account.platform,
            "caption": s.caption,
            "published_at": s.platform_post.published_at,
            "engagements": s.engagements,
            "lift": round(s.lift, 2),
        }
        for s in best
    ]


@router.get("/competitors", response=list[CompetitorOut], summary="Tracked competitors and their top recent posts")
def competitors(request, posts: int = Query(5, ge=0, le=30)):
    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "view_analytics")
    out = []
    for c in Competitor.objects.filter(workspace=request.workspace):
        top = CompetitorPost.objects.filter(competitor=c).order_by("-engagements")[:posts] if posts else []
        out.append(
            {
                "platform": c.platform,
                "handle": c.handle,
                "display_name": c.display_name,
                "followers": c.followers,
                "posts_count": c.posts_count,
                "last_synced_at": c.last_synced_at,
                "top_posts": [
                    {
                        "url": p.url,
                        "text": p.text,
                        "published_at": p.published_at,
                        "likes": p.likes,
                        "replies": p.replies,
                        "reposts": p.reposts,
                    }
                    for p in top
                ],
            }
        )
    log_audit_entry(request, action="growth.read.competitors", target_id=None, status_code=200)
    return out


@router.post("/utm", response=UtmOut, summary="Apply the workspace's UTM settings to text")
def utm(request, payload: UtmIn):
    """Tags links the same way publishing does, even when publish-time tagging is off."""
    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "view_analytics")
    settings = GrowthSettings.for_workspace(request.workspace)
    params = utm_params(
        source=settings.utm_source,
        medium=settings.utm_medium,
        campaign=settings.utm_campaign,
        platform=payload.platform or "social",
    )
    return {"text": add_utm(payload.text[:20000], params)}


# --- inspiration (browser extension) -------------------------------------------------------
#
# The request/response shape below is a contract shared with the Chrome
# extension. Keep field names and types stable.


class CaptureItemIn(Schema):
    id: str = Field(..., min_length=1, max_length=255)
    url: str = Field("", max_length=500)
    authorHandle: str = Field("", max_length=255)  # noqa: N815 — extension contract
    authorName: str = Field("", max_length=255)  # noqa: N815
    text: str = Field("", max_length=10000)
    # Every count is nullable: null means "the page didn't show it", never 0.
    likes: int | None = Field(None, ge=0)
    comments: int | None = Field(None, ge=0)
    shares: int | None = Field(None, ge=0)
    views: int | None = Field(None, ge=0)
    saves: int | None = Field(None, ge=0)
    mediaType: Literal["image", "video", "carousel"] | None = None  # noqa: N815
    postedAt: str | None = Field(None, max_length=64)  # noqa: N815
    own: bool = False
    manual: bool = False


class CaptureIn(Schema):
    platform: Literal["instagram", "tiktok"]
    ownHandle: str | None = Field(None, max_length=255)  # noqa: N815
    utcOffset: int | None = Field(None, ge=-14 * 60, le=14 * 60)  # noqa: N815 — JS getTimezoneOffset(): minutes west of UTC
    items: list[CaptureItemIn] = Field(..., max_length=100)


class CaptureOut(Schema):
    saved: int
    manual: int
    savedToday: int  # noqa: N815


class PingOut(Schema):
    ok: bool
    workspace: str


@router.get("/inspiration/ping", response=PingOut, summary="Check an extension's API key")
def inspiration_ping(request):
    """Used by the extension's "Test connection" button. Any valid key works."""
    enforce_http_rate_limits(request, is_write=False)
    return {"ok": True, "workspace": request.workspace.name}


@router.post("/inspiration/capture", response=CaptureOut, summary="Capture Instagram/TikTok posts from the extension")
def inspiration_capture(request, payload: CaptureIn):
    """Feed the inspiration library and the user's voice.

    * ``own`` items (or items by ``ownHandle``) go to the voice for
      ``(platform, ownHandle)``, never to the library.
    * ``manual`` items are saved as they are.
    * Everything else updates its author's like baseline and is saved only
      when it beats that author's usual likes (≥ 50 likes and ≥ 3× the median
      of at least 3 other posts); a null ``likes`` is never judged or sampled.
    """
    from apps.growth.services import inspiration
    from apps.growth.tasks import queue_tagging

    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "view_analytics")
    workspace = request.workspace
    items = [
        normalized
        for normalized in (inspiration.normalize_item(payload.platform, item.model_dump()) for item in payload.items)
        if normalized is not None
    ]
    result = inspiration.capture(workspace, payload.platform, items, own_handle=payload.ownHandle or "")
    if result.new_ids:
        queue_tagging(workspace)
    log_audit_entry(request, action="growth.inspiration.capture", target_id=None, status_code=200)
    return {
        "saved": result.saved,
        "manual": result.manual,
        "savedToday": inspiration.saved_today(workspace, -payload.utcOffset if payload.utcOffset is not None else None),
    }

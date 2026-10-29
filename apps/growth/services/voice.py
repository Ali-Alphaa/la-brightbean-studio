"""Per-account voice: own posts pulled from the official APIs, and a Claude style guide.

* Instagram (Facebook Login): ``GET /{ig-user-id}/media`` on graph.facebook.com.
* Instagram (Instagram Login): ``GET /me/media`` on graph.instagram.com.
* TikTok: ``POST /v2/video/list/`` — only when the account was connected with
  the ``video.list`` scope (requested when TikTok analytics is enabled).
  Without it, own TikTok posts come from the browser extension.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from django.utils import timezone

from . import ai
from .inspiration import InspirationError, _count, _parse_dt, add_own_samples, normalize_handle

logger = logging.getLogger(__name__)

IG_MEDIA_FIELDS = "id,caption,like_count,comments_count,media_type,permalink,timestamp"
TIKTOK_VIDEO_FIELDS = "id,title,video_description,like_count,comment_count,share_count,view_count,create_time,share_url"
PULL_LIMIT = 50


def voice_platform(account) -> str:
    return "tiktok" if account.platform == "tiktok" else "instagram"


def voice_handle(account) -> str:
    platform = voice_platform(account)
    return normalize_handle(platform, account.account_handle) or normalize_handle(platform, account.account_name)


def _instagram_items(provider, token: str, account) -> list[dict]:
    from providers.instagram import BASE_URL
    from providers.instagram_login import API_BASE

    url = (
        f"{BASE_URL}/{account.account_platform_id}/media" if account.platform == "instagram" else f"{API_BASE}/me/media"
    )
    data = provider._request(
        "GET", url, access_token=token, params={"fields": IG_MEDIA_FIELDS, "limit": PULL_LIMIT}
    ).json()
    return [
        {
            "external_id": str(m["id"]),
            "caption": str(m.get("caption") or ""),
            "likes": _count(m.get("like_count")),
            "comments": _count(m.get("comments_count")),
            "url": str(m.get("permalink") or ""),
            "posted_at": _parse_dt(m.get("timestamp")),
        }
        for m in data.get("data") or []
        if m.get("id")
    ]


def _tiktok_items(provider, token: str) -> list[dict]:
    from providers.tiktok import API_BASE

    data = provider._request(
        "POST",
        f"{API_BASE}/video/list/",
        access_token=token,
        params={"fields": TIKTOK_VIDEO_FIELDS},
        json={"max_count": 20},
    ).json()
    items = []
    for v in (data.get("data") or {}).get("videos") or []:
        if not v.get("id"):
            continue
        created = v.get("create_time")
        items.append(
            {
                "external_id": str(v["id"]),
                "caption": str(v.get("video_description") or v.get("title") or ""),
                "likes": _count(v.get("like_count")),
                "comments": _count(v.get("comment_count")),
                "views": _count(v.get("view_count")),
                "url": str(v.get("share_url") or ""),
                "posted_at": datetime.fromtimestamp(int(created), UTC) if created else None,
            }
        )
    return items


def pull_recent_posts(account):
    """Pull the account's recent posts into its voice. Returns ``(voice, count)``."""
    from apps.publisher.engine import _provider_and_access_token
    from providers.exceptions import ProviderError

    if account.platform not in ("instagram", "instagram_login", "tiktok"):
        raise InspirationError("Only Instagram and TikTok accounts have a voice here.")
    handle = voice_handle(account)
    if not handle:
        raise InspirationError("This account has no username on record; reconnect it first.")
    try:
        provider, token = _provider_and_access_token(account)
        items = (
            _tiktok_items(provider, token)
            if account.platform == "tiktok"
            else _instagram_items(provider, token, account)
        )
    except ProviderError as exc:
        logger.info("Pulling own posts for %s failed: %s", account.id, exc)
        if account.platform == "tiktok":
            raise InspirationError(
                "TikTok didn't allow reading your videos. That needs the video.list scope (enable TikTok "
                "analytics and reconnect); until then, open your TikTok profile with the browser extension."
            ) from exc
        raise InspirationError(f"Instagram didn't return your posts: {exc}") from exc
    voice = add_own_samples(
        workspace=account.workspace,
        platform=voice_platform(account),
        handle=handle,
        items=items,
        social_account=account,
    )
    return voice, len(items)


def best_samples(voice, limit: int = 25) -> list[dict]:
    """Own posts with the most likes (then comments) first."""
    return sorted(
        (s for s in voice.samples if s.get("text")),
        key=lambda s: ((s.get("likes") or 0), (s.get("comments") or 0)),
        reverse=True,
    )[:limit]


def regenerate_style_guide(voice) -> str:
    samples = [{"text": s["text"], "likes": s.get("likes"), "comments": s.get("comments")} for s in best_samples(voice)]
    voice.style_guide = ai.style_guide(samples, platform=voice.platform, handle=voice.handle)
    voice.style_guide_generated_at = timezone.now()
    voice.save(update_fields=["style_guide", "style_guide_generated_at", "updated_at"])
    return voice.style_guide


def default_voice_for(workspace, account):
    """The voice that belongs to a posting account, if one exists."""
    from apps.growth.models import GrowthVoice

    if account is None:
        return None
    by_account = GrowthVoice.objects.filter(workspace=workspace, social_account=account).first()
    if by_account:
        return by_account
    return GrowthVoice.objects.filter(
        workspace=workspace, platform=voice_platform(account), handle=voice_handle(account)
    ).first()

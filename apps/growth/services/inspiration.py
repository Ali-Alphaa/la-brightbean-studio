"""Instagram + TikTok inspiration library.

Posts reach the library three ways:

* :func:`capture` — the browser extension reports posts the user scrolled past;
  only posts that beat their author's usual likes (see :mod:`.viral`) or that
  the user saved by hand are kept. The user's *own* posts feed their voice.
* :func:`save_by_link` — a pasted Instagram post/reel or TikTok video URL.
* :func:`discover_instagram` — Instagram Business Discovery for a public
  business/creator account, via the workspace's connected Instagram account.

Every outbound request goes to a fixed host; the only user input that reaches
one is a URL that has already passed :func:`parse_post_url`, sent as a query
parameter. Redirects are never followed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import median
from urllib.parse import urlsplit

import httpx
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import viral

logger = logging.getLogger(__name__)

PLATFORMS = ("instagram", "tiktok")
MEDIA_TYPES = ("image", "video", "carousel")
TIMEOUT_SECONDS = 10
TAG_BATCH_SIZE = 20
#: Own posts kept per voice.
MAX_VOICE_SAMPLES = 60

TIKTOK_OEMBED_URL = "https://www.tiktok.com/oembed"
GRAPH_BASE = "https://graph.facebook.com/v25.0"

_IG_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
_TT_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
_TT_SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}
_IG_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9._]{1,30}/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]{5,64})/?$")
_TT_PATH_RE = re.compile(r"^/@([A-Za-z0-9._]{1,64})/(?:video|photo)/(\d{5,30})/?$")
_IG_HANDLE_RE = re.compile(r"^[a-z0-9._]{1,30}$")
_TT_HANDLE_RE = re.compile(r"^[a-z0-9._]{1,64}$")


class InspirationError(Exception):
    """User-facing failure (bad link, account not found, platform error)."""


# --- helpers ---------------------------------------------------------------------------------


def normalize_handle(platform: str, raw: str | None) -> str:
    """Lowercase handle without ``@``; empty string when it isn't a valid handle."""
    handle = (raw or "").strip().lstrip("@").lower()
    pattern = _IG_HANDLE_RE if platform == "instagram" else _TT_HANDLE_RE
    return handle if pattern.match(handle) else ""


def _count(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    parsed: datetime | None = (
        value if isinstance(value, datetime) else parse_datetime(str(value).strip().replace("+0000", "+00:00"))
    )
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, UTC)
    return parsed


def start_of_local_day(utc_offset_minutes: int | None, now: datetime | None = None) -> datetime:
    """UTC instant at which "today" began for a viewer ``utc_offset_minutes`` east of UTC."""
    now = now or timezone.now()
    offset = timedelta(minutes=max(-14 * 60, min(14 * 60, int(utc_offset_minutes or 0))))
    local = (now + offset).replace(hour=0, minute=0, second=0, microsecond=0)
    return local - offset


# --- URL validation & oEmbed -----------------------------------------------------------------


@dataclass
class ParsedLink:
    platform: str
    external_id: str
    url: str
    author_handle: str = ""


def parse_post_url(raw: str) -> ParsedLink:
    """Validate a pasted post URL and return its canonical form.

    Accepts only https Instagram post/reel/tv URLs and TikTok video/photo URLs
    on the networks' own hosts, without credentials or ports. Raises
    :class:`InspirationError` otherwise — this is the SSRF gate.
    """
    text = (raw or "").strip()
    if not text or len(text) > 500:
        raise InspirationError("Paste an Instagram post/reel link or a TikTok video link.")
    if text.startswith("http://"):
        text = "https://" + text[len("http://") :]
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise InspirationError("That doesn't look like a valid link.") from exc
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        raise InspirationError("Paste an https link copied from Instagram or TikTok.")
    if host in _IG_HOSTS:
        match = _IG_PATH_RE.match(parts.path)
        if not match:
            raise InspirationError("That Instagram link isn't a post or reel.")
        code = match.group(1)
        return ParsedLink("instagram", code, f"https://www.instagram.com/p/{code}/")
    if host in _TT_SHORT_HOSTS:
        raise InspirationError("Open the short TikTok link in your browser and paste the full video URL.")
    if host in _TT_HOSTS:
        match = _TT_PATH_RE.match(parts.path)
        if not match:
            raise InspirationError("That TikTok link isn't a video.")
        handle, video_id = match.group(1).lower(), match.group(2)
        return ParsedLink("tiktok", video_id, f"https://www.tiktok.com/@{handle}/video/{video_id}", handle)
    raise InspirationError("Only instagram.com and tiktok.com links are supported.")


def parse_tiktok_oembed(data: dict) -> dict:
    """Pick caption and author out of TikTok's oEmbed answer."""
    if not isinstance(data, dict):
        return {}
    handle = str(data.get("author_unique_id") or "")
    if not handle:
        match = re.search(r"tiktok\.com/@([A-Za-z0-9._]+)", str(data.get("author_url") or ""))
        handle = match.group(1) if match else ""
    return {
        "caption": str(data.get("title") or "").strip(),
        "author_name": str(data.get("author_name") or "").strip(),
        "author_handle": normalize_handle("tiktok", handle),
        "external_id": str(data.get("embed_product_id") or ""),
        "media_type": "video",
    }


def parse_instagram_oembed(data: dict) -> dict:
    """Pick what Instagram's oEmbed still returns (author, sometimes the caption as ``title``)."""
    if not isinstance(data, dict):
        return {}
    return {
        "caption": str(data.get("title") or "").strip(),
        "author_name": str(data.get("author_name") or "").strip(),
        "author_handle": normalize_handle("instagram", str(data.get("author_name") or "")),
    }


def _get_json(url: str, params: dict) -> dict:
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise InspirationError("Couldn't reach the network. Try again later.") from exc
    if response.status_code in (400, 404):
        raise InspirationError("The post wasn't found (it may be private or deleted).")
    if response.status_code >= 300:
        raise InspirationError(f"The network returned an error ({response.status_code}).")
    try:
        data = response.json()
    except ValueError as exc:
        raise InspirationError("The network returned an unexpected response.") from exc
    return data if isinstance(data, dict) else {}


def fetch_tiktok_oembed(url: str) -> dict:
    return parse_tiktok_oembed(_get_json(TIKTOK_OEMBED_URL, {"url": url}))


def instagram_app_token(workspace) -> str:
    """``app_id|app_secret`` of the workspace's Meta app, or '' when none is configured."""
    from apps.credentials.models import resolve_platform_credentials

    creds = resolve_platform_credentials("instagram", workspace.organization_id)
    app_id = creds.get("app_id") or creds.get("client_id") or ""
    secret = creds.get("app_secret") or creds.get("client_secret") or ""
    return f"{app_id}|{secret}" if app_id and secret else ""


def fetch_instagram_oembed(url: str, app_token: str) -> dict:
    data = _get_json(
        f"{GRAPH_BASE}/instagram_oembed",
        {"url": url, "omitscript": "true", "access_token": app_token},
    )
    return parse_instagram_oembed(data)


# --- saving ----------------------------------------------------------------------------------


def _save_post(workspace, platform: str, item: dict, *, source: str, ratio: float | None):
    """Create or refresh one library row. Returns ``(post, created)``."""
    from apps.growth.models import InspirationPost

    defaults = {
        "url": item.get("url", "")[:500],
        "author_handle": item.get("author_handle", "")[:255],
        "author_name": item.get("author_name", "")[:255],
        "caption": item.get("caption", ""),
        "likes": item.get("likes"),
        "comments": item.get("comments"),
        "shares": item.get("shares"),
        "views": item.get("views"),
        "saves": item.get("saves"),
        "media_type": item.get("media_type") or "",
        "posted_at": item.get("posted_at"),
    }
    # Never blank out what an earlier capture knew.
    defaults = {k: v for k, v in defaults.items() if v not in (None, "")}
    if ratio is not None:
        defaults["ratio"] = ratio
    post, created = InspirationPost.objects.get_or_create(
        workspace=workspace,
        platform=platform,
        external_id=item["external_id"][:255],
        defaults={**defaults, "source": source},
    )
    if not created:
        for key, value in defaults.items():
            setattr(post, key, value)
        if source == "manual" and post.source == "auto":
            post.source = "manual"
        post.save()
    return post, created


def _baseline(workspace, platform: str, handle: str):
    from apps.growth.models import AuthorBaseline

    obj, _ = AuthorBaseline.objects.select_for_update().get_or_create(
        workspace=workspace, platform=platform, handle=handle
    )
    return obj


def add_own_samples(workspace, platform: str, handle: str, items: list[dict], *, social_account=None):
    """Merge own posts into the voice for ``(platform, handle)``. Returns the voice."""
    from apps.growth.models import GrowthVoice

    with transaction.atomic():
        voice, _ = GrowthVoice.objects.select_for_update().get_or_create(
            workspace=workspace, platform=platform, handle=handle
        )
        by_id = {str(s.get("id")): s for s in voice.samples}
        for item in items:
            text = (item.get("caption") or "").strip()
            if not text:
                continue
            posted_at = item.get("posted_at")
            by_id[str(item["external_id"])] = {
                "id": str(item["external_id"]),
                "text": text[:5000],
                "likes": item.get("likes"),
                "comments": item.get("comments"),
                "views": item.get("views"),
                "url": item.get("url", ""),
                "posted_at": posted_at.isoformat() if isinstance(posted_at, datetime) else posted_at,
            }
        samples = sorted(by_id.values(), key=lambda s: s.get("posted_at") or "", reverse=True)
        voice.samples = samples[:MAX_VOICE_SAMPLES]
        if social_account is not None and voice.social_account_id is None:
            voice.social_account = social_account
        voice.save()
    return voice


def _match_social_account(workspace, platform: str, handle: str):
    from apps.social_accounts.models import SocialAccount

    platforms = ["instagram", "instagram_login"] if platform == "instagram" else ["tiktok"]
    for account in SocialAccount.objects.filter(workspace=workspace, platform__in=platforms):
        if normalize_handle(platform, account.account_handle or account.account_name) == handle:
            return account
    return None


@dataclass
class CaptureResult:
    saved: int = 0
    manual: int = 0
    own: int = 0
    new_ids: list[int] = field(default_factory=list)


def normalize_item(platform: str, raw: dict) -> dict | None:
    """Turn one extension item (camelCase contract) into internal field names."""
    external_id = str(raw.get("id") or "").strip()
    if not external_id:
        return None
    media_type = str(raw.get("mediaType") or "").lower()
    return {
        "external_id": external_id[:255],
        "url": str(raw.get("url") or "")[:500] if str(raw.get("url") or "").startswith("https://") else "",
        "author_handle": normalize_handle(platform, raw.get("authorHandle")),
        "author_name": str(raw.get("authorName") or "")[:255],
        "caption": str(raw.get("text") or ""),
        "likes": _count(raw.get("likes")),
        "comments": _count(raw.get("comments")),
        "shares": _count(raw.get("shares")),
        "views": _count(raw.get("views")),
        "saves": _count(raw.get("saves")),
        "media_type": media_type if media_type in MEDIA_TYPES else "",
        "posted_at": _parse_dt(raw.get("postedAt")),
        "own": bool(raw.get("own")),
        "manual": bool(raw.get("manual")),
    }


def capture(workspace, platform: str, items: list[dict], *, own_handle: str = "") -> CaptureResult:
    """Apply one extension capture batch (items already normalized).

    Own posts feed the voice of ``(platform, own_handle)``; manual saves are
    always kept; everything else is judged by the viral rule against the
    author's baseline, which every item with a like count updates.
    """
    result = CaptureResult()
    own_handle = normalize_handle(platform, own_handle)

    own_items = [i for i in items if i["own"] or (own_handle and i["author_handle"] == own_handle)]
    others = [i for i in items if i not in own_items]
    if own_items:
        handle = own_handle or own_items[0]["author_handle"]
        if handle:
            add_own_samples(
                workspace,
                platform,
                handle,
                own_items,
                social_account=_match_social_account(workspace, platform, handle),
            )
            result.own = len(own_items)

    by_author: dict[str, list[dict]] = {}
    for item in others:
        by_author.setdefault(item["author_handle"], []).append(item)

    for handle, author_items in by_author.items():
        with transaction.atomic():
            baseline = _baseline(workspace, platform, handle) if handle else None
            samples = list(baseline.samples) if baseline else []
            pending = list(baseline.pending) if baseline else []
            for item in author_items:
                samples = viral.add_sample(samples, item["external_id"], item["likes"])
            for item in author_items:
                verdict = viral.decide(item["likes"], samples, item["external_id"])
                post_ratio = viral.ratio(item["likes"], samples, item["external_id"])
                if item["manual"]:
                    post, created = _save_post(workspace, platform, item, source="manual", ratio=post_ratio)
                    result.manual += 1
                elif verdict == viral.VIRAL:
                    post, created = _save_post(workspace, platform, item, source="auto", ratio=post_ratio)
                elif verdict == viral.PENDING and handle:
                    pending = viral.add_pending(pending, _pending_entry(item))
                    continue
                else:
                    continue
                result.saved += 1
                if created:
                    result.new_ids.append(post.id)
            if baseline is not None:
                now_viral, pending = viral.resolve_pending(pending, samples)
                for entry in now_viral:
                    item = _from_pending(entry)
                    post_ratio = viral.ratio(item["likes"], samples, item["external_id"])
                    post, created = _save_post(workspace, platform, item, source="auto", ratio=post_ratio)
                    result.saved += 1
                    if created:
                        result.new_ids.append(post.id)
                baseline.samples = samples
                baseline.pending = pending
                baseline.save()
    return result


def _pending_entry(item: dict) -> dict:
    entry = {k: v for k, v in item.items() if k not in ("own", "manual")}
    entry["id"] = item["external_id"]
    entry["caption"] = entry["caption"][:5000]
    if isinstance(entry.get("posted_at"), datetime):
        entry["posted_at"] = entry["posted_at"].isoformat()
    return entry


def _from_pending(entry: dict) -> dict:
    item = {k: v for k, v in entry.items() if k != "id"}
    item["posted_at"] = _parse_dt(item.get("posted_at"))
    return item


def saved_today(workspace, utc_offset_minutes: int | None) -> int:
    from apps.growth.models import InspirationPost

    return InspirationPost.objects.filter(
        workspace=workspace, captured_at__gte=start_of_local_day(utc_offset_minutes)
    ).count()


def save_by_link(workspace, url: str, *, pasted_caption: str = ""):
    """Save one post from its link. Returns ``(post, note)``; ``note`` explains any missing data."""
    link = parse_post_url(url)
    note = ""
    item: dict = {"external_id": link.external_id, "url": link.url, "author_handle": link.author_handle}
    if link.platform == "tiktok":
        try:
            meta = fetch_tiktok_oembed(link.url)
            item.update({k: v for k, v in meta.items() if v and k != "external_id"})
        except InspirationError as exc:
            note = f"Couldn't read the TikTok post ({exc}); saved with the caption you pasted."
    else:
        item["media_type"] = ""
        token = instagram_app_token(workspace)
        if token:
            try:
                meta = fetch_instagram_oembed(link.url, token)
                item.update({k: v for k, v in meta.items() if v})
            except InspirationError as exc:
                note = f"Instagram oEmbed failed ({exc}); saved with the caption you pasted."
        else:
            note = (
                "No Meta app is configured, so Instagram details can't be fetched; saved with the caption you pasted."
            )
    if pasted_caption.strip() and not item.get("caption"):
        item["caption"] = pasted_caption.strip()
    if not item.get("caption"):
        raise InspirationError(note or "Couldn't read the caption. Paste it into the caption box and save again.")
    post, _ = _save_post(workspace, link.platform, item, source="link", ratio=None)
    return post, note


# --- Instagram Business Discovery ------------------------------------------------------------

DISCOVERY_FIELDS = (
    "business_discovery.username({handle}){{username,name,followers_count,"
    "media.limit(50){{id,caption,like_count,comments_count,media_type,permalink,timestamp}}}}"
)
_DISCOVERY_MEDIA_TYPES = {"IMAGE": "image", "VIDEO": "video", "CAROUSEL_ALBUM": "carousel", "REELS": "video"}


def parse_discovery(data: dict, handle: str) -> tuple[dict, list[dict]]:
    """Profile and per-post items (with ``ratio`` vs that author's median) from a Business Discovery answer."""
    bd = (data or {}).get("business_discovery") or {}
    username = str(bd.get("username") or handle)
    profile: dict = {
        "username": username,
        "name": str(bd.get("name") or ""),
        "followers": _count(bd.get("followers_count")),
    }
    author = normalize_handle("instagram", username) or handle
    items: list[dict] = []
    for media in (bd.get("media") or {}).get("data") or []:
        media_id = str(media.get("id") or "")
        if not media_id:
            continue
        items.append(
            {
                "external_id": media_id,
                "url": str(media.get("permalink") or "")[:500],
                "author_handle": author,
                "author_name": profile["name"],
                "caption": str(media.get("caption") or ""),
                "likes": _count(media.get("like_count")),
                "comments": _count(media.get("comments_count")),
                "media_type": _DISCOVERY_MEDIA_TYPES.get(str(media.get("media_type") or ""), ""),
                "posted_at": _parse_dt(media.get("timestamp")),
            }
        )
    likes = [i["likes"] for i in items if i["likes"] is not None]
    for item in items:
        others = list(likes)
        if item["likes"] is not None:
            others.remove(item["likes"])
        item["ratio"] = (
            round(item["likes"] / max(median(others), 1), 2) if others and item["likes"] is not None else None
        )
    items.sort(key=lambda i: i["ratio"] or 0, reverse=True)
    return profile, items


def discovery_account(workspace):
    """The connected Instagram (Facebook Login) account Business Discovery runs through."""
    from apps.social_accounts.models import SocialAccount

    return (
        SocialAccount.objects.filter(
            workspace=workspace, platform="instagram", connection_status=SocialAccount.ConnectionStatus.CONNECTED
        )
        .order_by("connected_at")
        .first()
    )


def discover_instagram(workspace, raw_handle: str) -> tuple[dict, list[dict]]:
    """Fetch an Instagram business/creator account's last 50 posts and update its baseline."""
    from apps.publisher.engine import _provider_and_access_token
    from providers.exceptions import ProviderError

    handle = normalize_handle("instagram", raw_handle)
    if not handle:
        raise InspirationError("Enter an Instagram username like @tradingview.")
    account = discovery_account(workspace)
    if account is None:
        raise InspirationError(
            "Connect an Instagram business account with Facebook Login first — Business Discovery runs through it."
        )
    try:
        provider, token = _provider_and_access_token(account)
        data = provider._request(
            "GET",
            f"{GRAPH_BASE}/{account.account_platform_id}",
            access_token=token,
            params={"fields": DISCOVERY_FIELDS.format(handle=handle)},
        ).json()
    except ProviderError as exc:
        logger.info("Business Discovery for @%s failed: %s", handle, exc)
        raise InspirationError(
            "Instagram couldn't look that account up. Only public business or creator accounts can be collected."
        ) from exc
    except httpx.HTTPError as exc:
        raise InspirationError("Couldn't reach Instagram. Try again later.") from exc
    profile, items = parse_discovery(data, handle)
    with transaction.atomic():
        baseline = _baseline(workspace, "instagram", handle)
        samples = list(baseline.samples)
        for item in sorted(items, key=lambda i: i["posted_at"] or timezone.now()):
            samples = viral.add_sample(samples, item["external_id"], item["likes"])
        baseline.samples = samples
        baseline.save()
    return profile, items


def save_discovered(workspace, items: list[dict]) -> int:
    saved = 0
    for item in items:
        _save_post(workspace, "instagram", item, source="discovery", ratio=item.get("ratio"))
        saved += 1
    return saved


# --- tagging -----------------------------------------------------------------------------------


def analyze_pending(workspace, *, max_batches: int = 5) -> int:
    """Tag untagged posts with Claude in batches of 20. Returns how many were tagged.

    Raises :class:`~apps.growth.services.ai.AIUnavailableError` when the AI isn't usable.
    """
    from apps.growth.models import InspirationPost

    from . import ai

    tagged = 0
    for _ in range(max_batches):
        batch = list(
            InspirationPost.objects.filter(workspace=workspace, analyzed_at__isnull=True, hidden=False).order_by(
                "-captured_at"
            )[:TAG_BATCH_SIZE]
        )
        if not batch:
            break
        tags = ai.tag_posts(
            [
                {"id": str(p.id), "platform": p.platform, "media_type": p.media_type, "caption": p.caption[:3000]}
                for p in batch
            ]
        )
        by_id = {t.id: t for t in tags}
        now = timezone.now()
        for post in batch:
            tag = by_id.get(str(post.id))
            if tag is not None:
                post.topics, post.hook_type, post.format, post.on_niche = (
                    tag.topics,
                    tag.hook_type,
                    tag.format,
                    tag.on_niche,
                )
                tagged += 1
            # Stamped either way so one post the model skips can't stall the queue.
            post.analyzed_at = now
            post.save(update_fields=["topics", "hook_type", "format", "on_niche", "analyzed_at"])
    return tagged

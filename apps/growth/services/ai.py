"""Optional AI helpers: Claude when ``ANTHROPIC_API_KEY`` is set, otherwise any
OpenAI-compatible chat-completions endpoint (``GROWTH_AI_API_KEY``).

Callers check :func:`is_enabled` first; every helper raises :class:`AIUnavailableError`
when it isn't configured or the call fails, so views can show a plain message.
Each helper describes its answer with a Pydantic model: Claude is asked for
exactly that schema (structured outputs), the OpenAI-compatible path for JSON
that is then validated against it.
"""

from __future__ import annotations

import json
import logging
import re

import httpx
from django.conf import settings
from pydantic import BaseModel, ValidationError

from . import claude

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30


#: The workspace owner's niche, used to flag on-/off-niche inspiration.
NICHE = "trading & markets (forex, stocks, crypto, options, trading psychology, risk)"


class AIUnavailableError(Exception):
    pass


def backend() -> str:
    """``"anthropic"``, ``"openai"`` or ``""`` (not configured)."""
    if claude.is_configured():
        return "anthropic"
    if getattr(settings, "GROWTH_AI_API_KEY", ""):
        return "openai"
    return ""


def is_enabled() -> bool:
    return bool(backend())


def _ask[T: BaseModel](system: str, user: str, schema: type[T], *, effort: str = "medium") -> T:
    """One structured answer from whichever backend is configured."""
    if backend() == "anthropic":
        return claude.structured(system, user, schema, effort=effort)
    data = _chat_json(system, user)
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        logger.warning("Growth AI answer didn't match %s: %s", schema.__name__, exc)
        raise AIUnavailableError("The AI service didn't return a usable answer. Try again in a moment.") from exc


def _chat_json(system: str, user: str) -> dict:
    """OpenAI-compatible chat-completions call returning a JSON object."""
    if not getattr(settings, "GROWTH_AI_API_KEY", ""):
        raise AIUnavailableError("AI is not configured. Set ANTHROPIC_API_KEY or GROWTH_AI_API_KEY to enable it.")
    try:
        response = httpx.post(
            f"{settings.GROWTH_AI_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.GROWTH_AI_API_KEY}"},
            json={
                "model": settings.GROWTH_AI_MODEL,
                "temperature": 0.7,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        logger.warning("Growth AI call failed: %s", exc)
        raise AIUnavailableError("The AI service didn't return a usable answer. Try again in a moment.") from exc


def _strings(value, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()][:limit]


class _Hashtags(BaseModel):
    hashtags: list[str]


class _Variants(BaseModel):
    variants: list[str]


class _Caption(BaseModel):
    caption: str


def suggest_hashtags(caption: str, *, platform: str = "", known_good: list[str] | None = None) -> list[str]:
    data = _ask(
        'You suggest social media hashtags. Reply with JSON: {"hashtags": ["tag", ...]} — '
        "8 tags at most, no '#' prefix, no spaces, mix of broad and niche, relevant to the caption.",
        json.dumps({"caption": caption, "platform": platform, "hashtags_that_worked_before": known_good or []}),
        _Hashtags,
    )
    tags = [t.lstrip("#").replace(" ", "") for t in _strings(data.hashtags, 8)]
    return [t for t in tags if t]


def caption_variants(caption: str, *, count: int = 2, platform: str = "") -> list[str]:
    data = _ask(
        "You write alternative versions of a social media caption for an A/B test. Keep the meaning, links "
        'and hashtags; vary the hook, structure and tone. Reply with JSON: {"variants": ["...", ...]}.',
        json.dumps({"caption": caption, "platform": platform, "count": count}),
        _Variants,
    )
    return _strings(data.variants, count)


def rewrite(caption: str, *, platform: str = "", style_guide: str = "", voice_samples: list[str] | None = None) -> str:
    """Refresh a caption; with a style guide/samples, rewrite it in that account's voice."""
    system = (
        "You refresh a social media post that performed well so it can be posted again without looking "
        'like a copy. Keep the facts, links and hashtags. Reply with JSON: {"caption": "..."}.'
    )
    if style_guide or voice_samples:
        system += (
            " Write it in the voice described by `style_guide` and shown in `voice_samples`; never copy a "
            "sample, and never add numbers, results or claims that aren't in the original."
        )
    data = _ask(
        system,
        json.dumps(
            {
                "caption": caption,
                "platform": platform,
                "style_guide": style_guide,
                "voice_samples": (voice_samples or [])[:10],
            }
        ),
        _Caption,
    )
    text = data.caption.strip()
    if not text:
        raise AIUnavailableError("The AI service returned an empty caption.")
    return text


# --- inspiration tagging -------------------------------------------------------------------

HOOK_TYPES = ["question", "contrarian", "story", "list", "stat", "how-to", "announcement", "other"]
FORMATS = ["one-liner", "carousel-tips", "tutorial", "talking-head", "story", "meme", "other"]


class PostTag(BaseModel):
    id: str
    topics: list[str]
    hook_type: str
    format: str
    on_niche: bool


class _Tags(BaseModel):
    posts: list[PostTag]


def tag_posts(posts: list[dict]) -> list[PostTag]:
    """Tag up to 20 posts (``{"id", "platform", "media_type", "caption"}``) in one call.

    Unknown hook/format values are coerced to ``"other"``; tags for ids that
    weren't asked about are dropped.
    """
    if not posts:
        return []
    data = _ask(
        "You label Instagram and TikTok posts for a content-inspiration library. For every post return: "
        "`topics` (1-3 short lowercase topics), "
        f"`hook_type` (one of: {', '.join(HOOK_TYPES)}), "
        f"`format` (one of: {', '.join(FORMATS)}), "
        f"and `on_niche` (true when the post is about {NICHE}). "
        'Reply with JSON: {"posts": [{"id", "topics", "hook_type", "format", "on_niche"}]}, one entry per '
        "input post, same ids.",
        json.dumps({"posts": posts}),
        _Tags,
        effort="low",
    )
    wanted = {str(p["id"]) for p in posts}
    out = []
    for tag in data.posts:
        if tag.id not in wanted:
            continue
        tag.topics = [t.strip().lower()[:40] for t in tag.topics if t.strip()][:3]
        tag.hook_type = tag.hook_type if tag.hook_type in HOOK_TYPES else "other"
        tag.format = tag.format if tag.format in FORMATS else "other"
        out.append(tag)
    return out


# --- repurpose: exactly 5 variations -----------------------------------------------------

VARIATION_COUNT = 5
CAPTION_LIMITS = {"instagram": 2200, "tiktok": 2200}
_HASHTAG_RE = re.compile(r"(?<!\w)#\w+")


class Variation(BaseModel):
    hook: str
    caption: str
    script: list[str]


class _Variations(BaseModel):
    variations: list[Variation]


def validate_variations(variations: list[Variation], *, platform: str, allow_hashtags: bool) -> list[dict]:
    """Check the model's answer: exactly 5, each with a caption (and a script for TikTok).

    Raises ``ValueError`` when the answer must be regenerated. Hashtags are
    removed when no source post used any.
    """
    if len(variations) != VARIATION_COUNT:
        raise ValueError(f"Expected {VARIATION_COUNT} variations, got {len(variations)}.")
    out = []
    for v in variations:
        caption = v.caption.strip()
        if not allow_hashtags:
            caption = re.sub(r"[ \t]{2,}", " ", _HASHTAG_RE.sub("", caption)).strip()
        if not caption:
            raise ValueError("A variation came back without a caption.")
        script = [beat.strip() for beat in v.script if beat.strip()] if platform == "tiktok" else []
        if platform == "tiktok" and not script:
            raise ValueError("A TikTok variation came back without a video script.")
        out.append({"hook": v.hook.strip(), "caption": caption, "script": script})
    return out


def repurpose(
    sources: list[dict],
    *,
    platform: str,
    style_guide: str = "",
    voice_samples: list[str] | None = None,
) -> list[dict]:
    """Five new posts carrying the same message and tone as ``sources``.

    ``sources`` are ``{"author", "caption", "media_type"}``. Retries once when
    the answer isn't exactly five usable variations.
    """
    allow_hashtags = any("#" in (s.get("caption") or "") for s in sources)
    platform_brief = (
        "Instagram: `hook` is the caption's first line (it shows before 'more'); `caption` is the full "
        "caption starting with that hook; `script` is an empty list."
        if platform == "instagram"
        else "TikTok: `hook` is the first sentence said on camera; `caption` is the post caption; `script` is "
        "a 15-45 second spoken video script as 4-8 short bullet beats, in speaking order."
    )
    system = (
        f"You repurpose high-performing social posts into new {platform} posts. Return exactly "
        f"{VARIATION_COUNT} variations that keep the source's core message and tone but use fresh wording "
        "and structure. Rules: never copy sentences verbatim; never invent numbers, results, prices, "
        "returns or claims that aren't in the sources; no financial promises. "
        + ("Hashtags are fine because the sources use them. " if allow_hashtags else "Do not use hashtags. ")
        + f"Captions must be under {CAPTION_LIMITS[platform]} characters. {platform_brief}"
    )
    if style_guide or voice_samples:
        system += (
            " Write in the account voice described by `style_guide` and shown in `voice_samples` "
            "(match it; never copy a sample)."
        )
    user = json.dumps(
        {
            "sources": sources,
            "style_guide": style_guide,
            "voice_samples": (voice_samples or [])[:10],
            "count": VARIATION_COUNT,
        }
    )
    last_error: ValueError | None = None
    for _attempt in range(2):
        data = _ask(system, user, _Variations, effort="medium")
        try:
            return validate_variations(data.variations, platform=platform, allow_hashtags=allow_hashtags)
        except ValueError as exc:
            logger.info("Repurpose answer rejected, retrying: %s", exc)
            last_error = exc
    raise AIUnavailableError(f"The AI didn't return {VARIATION_COUNT} usable variations. Try again.") from last_error


# --- voice -----------------------------------------------------------------------------------


class _StyleGuide(BaseModel):
    style_guide: str


def style_guide(samples: list[dict], *, platform: str, handle: str) -> str:
    """An editable style guide distilled from an account's best-performing posts."""
    if not samples:
        raise AIUnavailableError("Add some of your own posts first.")
    data = _ask(
        f"You write a concise style guide for the {platform} account @{handle} from its best-performing "
        "posts, so new captions can be written in the same voice. Cover: tone, point of view, sentence "
        "length, hooks, structure, emoji/hashtag habits, calls to action, words to use and avoid. "
        "Use short bullet points under plain headings; under 300 words. Describe the style; don't quote "
        'whole posts. Reply with JSON: {"style_guide": "..."}.',
        json.dumps({"posts": samples[:25]}),
        _StyleGuide,
        effort="medium",
    )
    text = data.style_guide.strip()
    if not text:
        raise AIUnavailableError("The AI service returned an empty style guide.")
    return text

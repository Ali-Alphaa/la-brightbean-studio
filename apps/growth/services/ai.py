"""Optional AI helpers over any OpenAI-compatible chat-completions endpoint.

Callers check :func:`is_enabled` first; every helper raises :class:`AIUnavailableError`
when it isn't configured or the call fails, so views can show a plain message.
"""

from __future__ import annotations

import json
import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30


class AIUnavailableError(Exception):
    pass


def is_enabled() -> bool:
    return bool(getattr(settings, "GROWTH_AI_API_KEY", ""))


def _chat_json(system: str, user: str) -> dict:
    if not is_enabled():
        raise AIUnavailableError("AI is not configured. Set GROWTH_AI_API_KEY to enable it.")
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


def suggest_hashtags(caption: str, *, platform: str = "", known_good: list[str] | None = None) -> list[str]:
    data = _chat_json(
        'You suggest social media hashtags. Reply with JSON: {"hashtags": ["tag", ...]} — '
        "8 tags at most, no '#' prefix, no spaces, mix of broad and niche, relevant to the caption.",
        json.dumps({"caption": caption, "platform": platform, "hashtags_that_worked_before": known_good or []}),
    )
    tags = [t.lstrip("#").replace(" ", "") for t in _strings(data.get("hashtags"), 8)]
    return [t for t in tags if t]


def caption_variants(caption: str, *, count: int = 2, platform: str = "") -> list[str]:
    data = _chat_json(
        "You write alternative versions of a social media caption for an A/B test. Keep the meaning, links "
        'and hashtags; vary the hook, structure and tone. Reply with JSON: {"variants": ["...", ...]}.',
        json.dumps({"caption": caption, "platform": platform, "count": count}),
    )
    return _strings(data.get("variants"), count)


def rewrite(caption: str, *, platform: str = "") -> str:
    data = _chat_json(
        "You refresh a social media post that performed well so it can be posted again without looking "
        'like a copy. Keep the facts, links and hashtags. Reply with JSON: {"caption": "..."}.',
        json.dumps({"caption": caption, "platform": platform}),
    )
    text = str(data.get("caption") or "").strip()
    if not text:
        raise AIUnavailableError("The AI service returned an empty caption.")
    return text

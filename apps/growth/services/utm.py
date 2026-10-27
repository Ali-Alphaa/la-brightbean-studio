"""UTM tagging of links in outgoing post text.

Applied at publish time so the stored caption stays exactly what the author
wrote. Deterministic (the same input always yields the same output), which the
first-comment duplicate check relies on, and idempotent: an existing ``utm_*``
parameter is never overwritten.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# Stop at whitespace and at characters that commonly wrap a link in prose.
URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
# Sentence punctuation directly after a link belongs to the sentence.
TRAILING_PUNCTUATION = ".,;:!?"


def utm_params(*, source: str, medium: str, campaign: str, platform: str) -> dict[str, str]:
    params = {
        "utm_source": (source or "").replace("{platform}", platform).strip(),
        "utm_medium": (medium or "").replace("{platform}", platform).strip(),
        "utm_campaign": (campaign or "").replace("{platform}", platform).strip(),
    }
    return {k: v for k, v in params.items() if v}


def tag_url(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    if not parts.netloc:
        return url
    query = parse_qsl(parts.query, keep_blank_values=True)
    present = {k.lower() for k, _ in query}
    additions = [(k, v) for k, v in params.items() if k.lower() not in present]
    if not additions:
        return url
    return urlunsplit(parts._replace(query=urlencode(query + additions)))


def add_utm(text: str, params: dict[str, str]) -> str:
    if not text or not params:
        return text

    def _replace(match: re.Match) -> str:
        url = match.group(0)
        trailing = ""
        while url and url[-1] in TRAILING_PUNCTUATION:
            trailing = url[-1] + trailing
            url = url[:-1]
        return tag_url(url, params) + trailing

    return URL_RE.sub(_replace, text)


def tag_for_platform_post(text: str | None, platform_post) -> str | None:
    """Apply the workspace's UTM settings to ``text`` for this platform post.

    Never raises: a tagging bug must not stop a post from publishing.
    """
    if not text or not URL_RE.search(text):
        return text  # nothing to tag; skip the settings lookup on every link-less post
    try:
        from apps.growth.models import GrowthSettings

        settings = GrowthSettings.objects.filter(workspace_id=platform_post.post.workspace_id).first()
        if settings is None or not settings.utm_enabled:
            return text
        params = utm_params(
            source=settings.utm_source,
            medium=settings.utm_medium,
            campaign=settings.utm_campaign,
            platform=platform_post.social_account.platform,
        )
        return add_utm(text, params)
    except Exception:
        logger.exception("UTM tagging failed for PlatformPost %s; publishing untagged", platform_post.id)
        return text

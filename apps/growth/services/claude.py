"""Claude (Anthropic) backend for the Growth AI helpers.

Used by :mod:`apps.growth.services.ai` whenever ``ANTHROPIC_API_KEY`` is set.
Every call asks for structured output validated against a Pydantic model and
returns that model, or raises :class:`~apps.growth.services.ai.AIUnavailableError`
with a sentence a view can show as-is.

Request shape, deliberately:

* no ``thinking`` / ``budget_tokens`` — the default model always thinks and
  rejects both with a 400; depth is set with ``output_config.effort`` instead;
* no ``temperature`` / ``top_p`` / ``top_k`` and no assistant prefill;
* server-side refusal fallback (``fallbacks="default"``) through the beta
  endpoint, retried once without it if the API rejects the beta.

:func:`get_client` is the one seam tests patch.
"""

from __future__ import annotations

import logging
import os

import anthropic
from django.conf import settings
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000
TIMEOUT_SECONDS = 180
FALLBACK_BETA = "server-side-fallback-2026-07-01"
EFFORTS = {"low", "medium"}


def is_configured() -> bool:
    return bool(getattr(settings, "ANTHROPIC_API_KEY", ""))


def model_name() -> str:
    return getattr(settings, "ANTHROPIC_MODEL", "") or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=TIMEOUT_SECONDS, max_retries=2)


def _unavailable(message: str, exc: Exception | None = None):
    from .ai import AIUnavailableError

    error = AIUnavailableError(message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _request(client: anthropic.Anthropic, kwargs: dict):
    """Beta endpoint with server-side fallback; plain ``messages.parse`` if the beta is refused."""
    try:
        return client.beta.messages.parse(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
    except anthropic.BadRequestError as exc:
        if "fallback" not in str(exc).lower():
            raise
        logger.warning("Claude rejected the server-side fallback beta; retrying without it: %s", exc)
        return client.messages.parse(**kwargs)


def structured[T: BaseModel](system: str, user: str, schema: type[T], *, effort: str = "medium") -> T:
    """Ask Claude for one JSON object matching ``schema``."""
    if effort not in EFFORTS:
        raise ValueError(f"effort must be one of {sorted(EFFORTS)}")
    kwargs = {
        "model": model_name(),
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_format": schema,
        "output_config": {"effort": effort},
    }
    try:
        response = _request(get_client(), kwargs)
    except anthropic.RateLimitError as exc:
        logger.warning("Claude rate limited the growth AI call: %s", exc)
        raise _unavailable("Claude is busy right now (rate limited). Try again in a minute.", exc) from exc
    except anthropic.AuthenticationError as exc:
        logger.error("Claude rejected ANTHROPIC_API_KEY: %s", exc)
        raise _unavailable("Claude rejected the API key. Check ANTHROPIC_API_KEY.", exc) from exc
    except anthropic.APIConnectionError as exc:
        logger.warning("Couldn't reach Claude: %s", exc)
        raise _unavailable("Couldn't reach Claude. Try again in a moment.", exc) from exc
    except anthropic.APIError as exc:
        logger.warning("Claude call failed: %s", exc)
        raise _unavailable("Claude didn't return a usable answer. Try again in a moment.", exc) from exc
    except (ValidationError, ValueError) as exc:  # output that doesn't match the schema
        logger.warning("Claude returned output that didn't match %s: %s", schema.__name__, exc)
        raise _unavailable("Claude didn't return a usable answer. Try again in a moment.", exc) from exc

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        logger.info("Claude declined a growth AI request (category=%s)", getattr(details, "category", None))
        raise _unavailable("Claude declined this request. Try different source posts or wording.")
    if response.stop_reason == "max_tokens":
        raise _unavailable("Claude's answer was cut off. Try fewer source posts.")
    parsed = response.parsed_output
    if parsed is None:
        raise _unavailable("Claude didn't return a usable answer. Try again in a moment.")
    return parsed

"""The Claude path of the growth AI service, with a mocked Anthropic client."""

from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx2
import pytest

from apps.growth.services import ai, claude


def _response(parsed, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed, stop_details=None)


def _api_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(
        "boom fallback" if status == 400 else "boom", response=httpx2.Response(status, request=request), body=None
    )


@pytest.fixture
def client(settings):
    settings.ANTHROPIC_API_KEY = "sk-ant-test"
    settings.ANTHROPIC_MODEL = ""
    settings.GROWTH_AI_API_KEY = ""
    fake = mock.MagicMock()
    with mock.patch.object(claude, "get_client", return_value=fake):
        yield fake


def test_backend_selection(settings):
    settings.ANTHROPIC_API_KEY = ""
    settings.GROWTH_AI_API_KEY = ""
    assert ai.backend() == "" and not ai.is_enabled()
    settings.GROWTH_AI_API_KEY = "sk-openai"
    assert ai.backend() == "openai" and ai.is_enabled()
    settings.ANTHROPIC_API_KEY = "sk-ant"
    assert ai.backend() == "anthropic"


def test_model_defaults_and_env_override(settings, monkeypatch):
    settings.ANTHROPIC_MODEL = ""
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    assert claude.model_name() == "claude-opus-5-5"
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    assert claude.model_name() == "claude-sonnet-5-5"


def test_request_shape_effort_and_no_thinking_or_sampling(client):
    client.beta.messages.parse.return_value = _response(ai._Tags(posts=[]))
    ai.tag_posts([{"id": "1", "platform": "instagram", "media_type": "image", "caption": "Risk 1% per trade"}])

    kwargs = client.beta.messages.parse.call_args.kwargs
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["max_tokens"] == 16000
    assert kwargs["output_format"] is ai._Tags
    assert kwargs["output_config"] == {"effort": "low"}  # tagging is low effort
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"
    for forbidden in ("thinking", "budget_tokens", "temperature", "top_p", "top_k"):
        assert forbidden not in kwargs
    assert [m["role"] for m in kwargs["messages"]] == ["user"]  # no assistant prefill


def test_repurpose_uses_medium_effort(client):
    five = [ai.Variation(hook="h", caption=f"c{i}", script=[]) for i in range(5)]
    client.beta.messages.parse.return_value = _response(ai._Variations(variations=five))
    out = ai.repurpose([{"author": "a", "caption": "x", "media_type": "image"}], platform="instagram")
    assert len(out) == 5
    assert client.beta.messages.parse.call_args.kwargs["output_config"] == {"effort": "medium"}


def test_refusal_raises_friendly_error(client):
    client.beta.messages.parse.return_value = _response(None, stop_reason="refusal")
    with pytest.raises(ai.AIUnavailableError, match="declined"):
        ai.style_guide([{"text": "hi"}], platform="instagram", handle="me")


def test_missing_parsed_output_raises(client):
    client.beta.messages.parse.return_value = _response(None)
    with pytest.raises(ai.AIUnavailableError):
        ai.rewrite("caption")


def test_rate_limit_and_api_errors_become_unavailable(client):
    client.beta.messages.parse.side_effect = _api_error(anthropic.RateLimitError, 429)
    with pytest.raises(ai.AIUnavailableError, match="busy"):
        ai.rewrite("caption")
    client.beta.messages.parse.side_effect = _api_error(anthropic.InternalServerError, 500)
    with pytest.raises(ai.AIUnavailableError):
        ai.rewrite("caption")


def test_rejected_fallback_beta_retries_without_it(client):
    client.beta.messages.parse.side_effect = _api_error(anthropic.BadRequestError, 400)
    client.messages.parse.return_value = _response(ai._Caption(caption="fresh"))
    assert ai.rewrite("old") == "fresh"
    kwargs = client.messages.parse.call_args.kwargs
    assert "fallbacks" not in kwargs and "betas" not in kwargs
    assert kwargs["output_config"] == {"effort": "medium"}


def test_tags_are_coerced_to_known_values(client):
    client.beta.messages.parse.return_value = _response(
        ai._Tags(
            posts=[
                ai.PostTag(id="1", topics=["Forex ", "", "Risk"], hook_type="weird", format="tutorial", on_niche=True),
                ai.PostTag(id="zzz", topics=[], hook_type="list", format="meme", on_niche=False),
            ]
        )
    )
    tags = ai.tag_posts([{"id": "1", "platform": "tiktok", "media_type": "video", "caption": "x"}])
    assert [(t.id, t.topics, t.hook_type, t.format) for t in tags] == [("1", ["forex", "risk"], "other", "tutorial")]


# --- exactly-5 validation ------------------------------------------------------------------


def _v(n, script=None, caption="Buy the dip? Not so fast."):
    return [ai.Variation(hook="Hook", caption=caption, script=script or []) for _ in range(n)]


def test_validate_variations_requires_exactly_five():
    assert len(ai.validate_variations(_v(5), platform="instagram", allow_hashtags=True)) == 5
    for n in (4, 6):
        with pytest.raises(ValueError):
            ai.validate_variations(_v(n), platform="instagram", allow_hashtags=True)


def test_validate_variations_tiktok_needs_script_and_strips_hashtags():
    with pytest.raises(ValueError):
        ai.validate_variations(_v(5), platform="tiktok", allow_hashtags=False)
    out = ai.validate_variations(
        _v(5, ["Beat 1", " "], "Risk first #trading #fx"), platform="tiktok", allow_hashtags=False
    )
    assert out[0] == {"hook": "Hook", "caption": "Risk first", "script": ["Beat 1"]}


def test_repurpose_retries_once_then_gives_up(client):
    good = ai._Variations(variations=_v(5))
    client.beta.messages.parse.side_effect = [_response(ai._Variations(variations=_v(3))), _response(good)]
    assert len(ai.repurpose([{"caption": "x"}], platform="instagram")) == 5
    assert client.beta.messages.parse.call_count == 2

    client.beta.messages.parse.reset_mock()
    client.beta.messages.parse.side_effect = [_response(ai._Variations(variations=_v(4)))] * 2
    with pytest.raises(ai.AIUnavailableError, match="5 usable"):
        ai.repurpose([{"caption": "x"}], platform="instagram")
    assert client.beta.messages.parse.call_count == 2


def test_openai_path_still_used_without_anthropic_key(settings):
    settings.ANTHROPIC_API_KEY = ""
    settings.GROWTH_AI_API_KEY = "sk-openai"
    with mock.patch.object(ai, "_chat_json", return_value={"caption": "from openai"}) as chat:
        assert ai.rewrite("x") == "from openai"
    chat.assert_called_once()


def test_real_sdk_serializes_the_request_we_expect(settings):
    """Run the installed SDK end to end against a mock transport (no network)."""
    import json

    settings.ANTHROPIC_API_KEY = "sk-ant-test"
    settings.ANTHROPIC_MODEL = ""
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta")
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": json.dumps({"caption": "Fresh take"})}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    real = anthropic.Anthropic(
        api_key="sk-ant-test",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    with mock.patch.object(claude, "get_client", return_value=real):
        assert ai.rewrite("old caption") == "Fresh take"

    body = seen["body"]
    assert seen["beta"] == "server-side-fallback-2026-07-01"
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert not {"thinking", "temperature", "top_p", "top_k"} & set(body)

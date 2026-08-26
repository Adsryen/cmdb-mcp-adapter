"""Secret redaction: key-based, value-based, and embedded-in-payload.

The rules (see :mod:`cmdb_mcp_adapter.observability.redaction`):

* Any mapping key matching a sensitive pattern (``token``, ``secret``,
  ``password``, ``authorization``, ``cookie``, ...) has its value
  replaced by ``<redacted>`` regardless of the value.
* String values that look like bearer tokens (long high-entropy, or a
  ``Bearer <...>`` prefix) are redacted wherever they appear, including
  nested inside large payloads.
* Caller-supplied known-secret strings are redacted by exact match and
  when embedded in longer strings.
"""

from __future__ import annotations

from cmdb_mcp_adapter.observability.redaction import (
    REDACTED,
    looks_like_secret,
    redact,
)

FAKE_TOKEN = "a1b2c3d4e5f6g7h8i9j0"
FAKE_LONG = "0" * 32


class TestKeyBased:
    def test_token_key(self) -> None:
        out = redact({"api_token": "whatever", "name": "x"})
        assert out["api_token"] == REDACTED
        assert out["name"] == "x"

    def test_authorization_key(self) -> None:
        out = redact({"Authorization": "Bearer xyz"})
        assert out["Authorization"] == REDACTED

    def test_password_key(self) -> None:
        out = redact({"password": "hunter2"})
        assert out["password"] == REDACTED

    def test_nested_key(self) -> None:
        out = redact({"outer": {"client_secret": "s", "other": "ok"}})
        assert out["outer"]["client_secret"] == REDACTED
        assert out["other" if False else "outer"]["other"] == "ok"

    def test_non_sensitive_key_preserved(self) -> None:
        out = redact({"message": "hello world"})
        assert out["message"] == "hello world"


class TestValueBased:
    def test_long_high_entropy_string(self) -> None:
        out = redact({"data": FAKE_LONG})
        assert out["data"] == REDACTED

    def test_bearer_prefix(self) -> None:
        # An embedded ``Bearer <token>`` is replaced by ``<redacted>``
        # within the surrounding string (the rest of the text is
        # preserved), so the raw token never appears.
        out = redact({"note": f"token is Bearer {FAKE_TOKEN}"})
        assert FAKE_TOKEN not in out["note"]
        assert REDACTED in out["note"]

    def test_short_string_preserved(self) -> None:
        out = redact({"note": "hello"})
        assert out["note"] == "hello"

    def test_url_preserved(self) -> None:
        out = redact({"url": "https://example.com/api/v1"})
        assert out["url"] == "https://example.com/api/v1"

    def test_known_secret_by_exact_match(self) -> None:
        out = redact({"v": "a-short-value"}, secret_values=["a-short-value"])
        assert out["v"] == REDACTED

    def test_known_secret_embedded_in_longer_string(self) -> None:
        payload = f"prefix-{FAKE_TOKEN}-suffix"
        out = redact({"v": payload}, secret_values=[FAKE_TOKEN])
        assert out["v"] == f"prefix-{REDACTED}-suffix"

    def test_nested_list_of_secrets(self) -> None:
        out = redact({"items": [FAKE_LONG, "ok"]}, secret_values=[FAKE_LONG])
        assert out["items"] == [REDACTED, "ok"]


class TestLooksLikeSecret:
    def test_long_token(self) -> None:
        # A 32-char unspaced token-shaped string is detected.
        assert looks_like_secret("a" * 32)

    def test_short_token(self) -> None:
        assert not looks_like_secret("abc")

    def test_medium_token_not_shape_detected_but_still_redactable(self) -> None:
        # A 20-char string is below the shape threshold, but a caller
        # can still protect it via secret_values (exact match).
        assert not looks_like_secret("a" * 20)
        out = redact({"v": "a" * 20}, secret_values=["a" * 20])
        assert out["v"] == REDACTED

    def test_url(self) -> None:
        assert not looks_like_secret("https://example.com")

    def test_prose(self) -> None:
        assert not looks_like_secret("hello world this is a sentence")

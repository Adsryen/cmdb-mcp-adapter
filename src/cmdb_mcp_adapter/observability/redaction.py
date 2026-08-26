"""Secret redaction for logs and diagnostics.

Two complementary rules protect against credential leakage:

* **Key-based** — any mapping key whose (lower-cased) name matches a
  sensitive pattern (``token``, ``secret``, ``password``,
  ``authorization``, ``cookie``, ``credential``, ...) has its value
  replaced by :data:`REDACTED` regardless of what the value is.
* **Value-based** — values that look like bearer/secret tokens (long
  high-entropy strings, ``Bearer ...`` prefixes, or a caller-supplied
  known-secret value) are replaced by :data:`REDACTED` wherever they
  appear, including *inside* large payloads (nested dicts / lists /
  strings) that were not flagged by key.

The rules are applied recursively, so a token embedded anywhere in a
large upstream payload is still redacted before the structure is
serialized into a log line.  :class:`RedactingFilter` wires the same
logic into the stdlib logging pipeline.

Nothing here prints, and the module is deliberately dependency-free so it
can run at startup before the transport is chosen.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from typing import Any

#: The literal that replaces any redacted secret value.
REDACTED = "<redacted>"

#: Lower-cased key substrings whose values are always redacted.
_SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "cookie",
    "credential",
    "private_key",
    "api_key",
    "apikey",
    "key_id",
    "_key",
)

#: Case-insensitive ``Bearer <value>`` occurrences inside string values.
_BEARER_RE = re.compile(r"(?i)bearer\s+[a-z0-9+/_\-\.=]{8,}")

#: A "token-like" standalone value: a single long unspaced high-entropy
#: string (hex / base64-ish) without any scheme prefix.
_TOKEN_VALUE_RE = re.compile(r"^[A-Za-z0-9+/_\-]{16,}={0,2}$")

#: Minimum length for a standalone value to be treated as token-shaped
#: by the value-based rule.  Deliberately high (>= 24) so that ordinary
#: identifiers, UUIDs, and short hashes are not over-redacted; exact
#: known secrets are handled separately via ``secret_values``.
_TOKEN_MIN_LENGTH = 24


def _key_is_sensitive(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


#: Per-secret word-boundary regex, built lazily by :func:`_secret_word_re`.
#: The boundary is a character *outside* the token-like alphabet (or the
#: start/end of the string) so a known secret that is a substring of a
#: longer identifier is not over-redacted.
_SECRET_WORD_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _secret_word_re(secret: str) -> re.Pattern[str]:
    """A word-bounded regex for *one* known secret.

    The boundary is "not preceded/followed by a token-like
    character" so ``secret=<val>`` matches but ``x<val>y`` (embedded in
    a longer identifier) does not.
    """
    cached = _SECRET_WORD_RE_CACHE.get(secret)
    if cached is not None:
        return cached
    pat = re.compile(
        r"(?<![A-Za-z0-9+/_\-])" + re.escape(secret) + r"(?![A-Za-z0-9+/_\-])"
    )
    _SECRET_WORD_RE_CACHE[secret] = pat
    return pat


def _string_looks_like_secret(value: str) -> bool:
    """Value-based rule: does this *string* look like a credential?

    Catches: ``Bearer <token>`` occurrences; and very long (>= 24 char)
    unspaced strings of a token-like alphabet.  URLs (which contain a
    ``://`` scheme) and ordinary short strings are left alone, so useful
    diagnostics survive.  The threshold is deliberately high: a 16-char
    hex fragment is too common to treat as a secret by shape alone; such
    values are instead protected by the caller-supplied ``secret_values``
    exact-match list.
    """
    if _BEARER_RE.search(value):
        return True
    if "://" in value:
        # A URL, not a bare token.
        return False
    if len(value) < _TOKEN_MIN_LENGTH:
        return False
    # A single unspaced run of token-like characters, long enough to be
    # a real credential, is token-shaped.
    return " " not in value and bool(_TOKEN_VALUE_RE.match(value))


def _redact_string(value: str, secret_values: frozenset[str]) -> str:
    # Exact full-string match against a known secret: redact wholesale.
    if value in secret_values:
        return REDACTED
    # A known secret embedded in a longer string: redact *every*
    # occurrence of *every* known secret, preserving surrounding
    # context.  The frozenset iteration order is unspecified, so the
    # loop must not return on the first match: a body that embeds two
    # registered secrets (e.g. the veops ``_key`` and ``_secret`` pair
    # echoed by a hostile upstream) must have both scrubbed.
    for secret in secret_values:
        if secret and secret in value:
            value = value.replace(secret, REDACTED)
    # Replace embedded Bearer occurrences even inside longer strings.
    if _BEARER_RE.search(value):
        return _BEARER_RE.sub(REDACTED, value)
    # Standalone value that is token-shaped (very long unspaced):
    # redact the whole value.
    if _string_looks_like_secret(value):
        return REDACTED
    # Known secrets embedded in *compact* contexts (JSON payloads, URL
    # query strings, header lines) without the exact-substring match
    # catching them: redact each occurrence with a word-boundary match
    # so a hostile upstream body that echoes a credential next to a
    # delimiter (``key=k1 secret=s2``) cannot carry the raw value
    # through, but a secret that is a substring of a longer identifier
    # is not over-redacted.
    for secret in secret_values:
        if not secret:
            continue
        pat = _secret_word_re(secret)
        if pat.search(value):
            value = pat.sub(REDACTED, value)
    return value


def redact(obj: Any, secret_values: Iterable[str] | None = None) -> Any:
    """Recursively redact a JSON-like structure for safe logging.

    ``obj`` may be any nested combination of dict / list / str / scalar.
    ``secret_values`` are exact known-secret strings (e.g. the configured
    tokens) that are additionally redacted wherever they appear as a value
    or embedded in a larger string.

    The input is not mutated; a redacted copy is returned.  The result is
    JSON-serializable only if the input was.
    """
    secrets = frozenset(s for s in (secret_values or ()) if s)
    return _redact_obj(obj, secrets)


def _redact_obj(obj: Any, secrets: frozenset[str]) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if _key_is_sensitive(k):
                out[str(k)] = REDACTED
            else:
                out[str(k)] = _redact_obj(v, secrets)
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact_obj(v, secrets) for v in obj]
    if isinstance(obj, str):
        return _redact_string(obj, secrets)
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    # Unknown object types: render via repr, then redact the string form
    # so a token carried in an exotic object cannot leak.
    return _redact_string(repr(obj), secrets)


def redact_mapping_keys(obj: Any) -> Any:
    """Key-based redaction only (no value-pattern analysis).

    Useful when a caller already knows the value shapes and only needs
    the named-secret protection.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if _key_is_sensitive(key):
                out[str(key)] = REDACTED
            else:
                out[str(key)] = redact_mapping_keys(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_mapping_keys(v) for v in obj]
    return obj


def looks_like_secret(value: str) -> bool:
    """Public helper: is this string token-shaped (value-based rule)?"""
    return _string_looks_like_secret(value)


class RedactingFilter:
    """A ``logging.Filter`` that redacts secrets from logged records.

    Attach to the root logger after
    :func:`cmdb_mcp_adapter.observability.logging.configure_logging`:
    ``logging.getLogger().addFilter(RedactingFilter(secrets))``.

    The filter redacts both the rendered message text and the structured
    ``extra`` fields, and re-renders the message so downstream formatters
    see the redacted text.  Records without extra fields pass through
    essentially untouched (only the message string is re-rendered).
    """

    def __init__(self, secret_values: Iterable[str] | None = None) -> None:
        self._secrets = frozenset(s for s in (secret_values or ()) if s)

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact the fully-formatted message text (after args
        # substitution) and write it back as a plain string so the
        # downstream formatter's ``getMessage()`` returns the redacted
        # value verbatim.
        formatted = record.getMessage()
        redacted = redact(formatted, self._secrets)
        if isinstance(redacted, str):
            record.msg = redacted
            record.args = None
        # Redact structured extra fields (anything beyond the standard
        # LogRecord attributes) so a dict stuffed into ``extra`` cannot
        # carry a raw secret past the filter.
        standard = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
        for key, value in list(record.__dict__.items()):
            if key in standard or key in ("msg", "args"):
                continue
            record.__dict__[key] = redact(value, self._secrets)
        return True


def serialize_for_log(obj: Any, secret_values: Iterable[str] | None = None) -> str:
    """Redact and JSON-serialize a value for embedding in a log line.

    Never raises: if the value is not JSON-serializable it falls back to
    its (redacted) ``repr`` so a bad payload can never take logging down.
    """
    redacted = redact(obj, secret_values)
    try:
        return json.dumps(redacted, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        return str(redacted)

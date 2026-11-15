"""Golden-vector unit tests for :mod:`...adapters.veops.signing`.

The vectors below were cross-checked against the live VeOps 2.6.2
deployment (intranet VeOps host, 2026-08-25): the identical computation over
the same path/secret/params produced a request the upstream accepted
(200 + data), while a wrong digest was rejected with
``401 {"message": "401 Unauthorized: unauthorized"}``.

Covered: empty params, multi-param sorting, non-ASCII (Chinese) values,
exclusion of ``_key``/``_secret``, exclusion of ``None``/container
values, and the ``signed_params`` assembly.
"""

from __future__ import annotations

from cmdb_mcp_adapter.adapters.veops import signing

SECRET = "0123456789abcdef0123456789abcdef"


class TestSignGoldenVectors:
    def test_empty_params(self) -> None:
        # sha1("/api/v0.1/ci_types" + SECRET)
        assert signing.sign("/api/v0.1/ci_types", SECRET, {}) == (
            "7a2046d18a4eb0e6a5546305cd3fd8fe5d60c6ba"
        )

    def test_multi_param_sorted_by_key(self) -> None:
        # sorted by key: ci_type, page, page_size, q
        # digest of path + secret + "7" + "1" + "20" + "ACC-0166"
        assert (
            signing.sign(
                "/api/v0.1/ci/search",
                SECRET,
                {"q": "ACC-0166", "page": 1, "page_size": 20, "ci_type": 7},
            )
            == "680a49a4723c1f78217f8a5beb11f620074975fb"
        )

    def test_chinese_and_empty_string_values(self) -> None:
        # UTF-8 encoding of the concatenated values must be stable.
        assert (
            signing.sign(
                "/api/v0.1/ci",
                SECRET,
                {"ci_type": 7, "quick_re_title": "快速查询", "empty": ""},
            )
            == "9e956cfd9bfa7eea8da9be287f2c470d916935b6"
        )

    def test_key_and_secret_params_are_excluded(self) -> None:
        # The signature never includes _key / _secret themselves.
        with_extras = {
            "_key": "some-key",
            "_secret": "deadbeef",
            "b": 2,
            "a": "x",
        }
        without_extras = {"b": 2, "a": "x"}
        assert (
            signing.sign("/api/v0.1/ci_types", SECRET, with_extras)
            == signing.sign("/api/v0.1/ci_types", SECRET, without_extras)
            == "eb98cc4e8c56b413c358aca978229fa78d901b3b"
        )

    def test_none_and_container_values_are_skipped(self) -> None:
        # None values are not sent on the wire and must not be signed;
        # dict/list values are not scalar and are skipped too.
        full = {"a": None, "b": [1, 2], "c": {"x": 1}, "d": 3}
        reduced = {"d": 3}
        assert (
            signing.sign("/api/v0.1/ci", SECRET, full)
            == signing.sign("/api/v0.1/ci", SECRET, reduced)
        )

    def test_signature_depends_on_secret(self) -> None:
        a = signing.sign("/api/v0.1/ci_types", "secret-a", {"x": 1})
        b = signing.sign("/api/v0.1/ci_types", "secret-b", {"x": 1})
        assert a != b


class TestSignedParams:
    def test_assembles_key_and_secret(self) -> None:
        out = signing.signed_params(
            "/api/v0.1/ci_types", "K", SECRET, {"page": 1}
        )
        assert out["_key"] == "K"
        expected = signing.sign("/api/v0.1/ci_types", SECRET, {"page": 1})
        assert out["_secret"] == expected
        assert out["page"] == 1

    def test_none_values_dropped(self) -> None:
        out = signing.signed_params("/api/v0.1/ci/search", "K", SECRET, {"q": None})
        assert "q" not in out


class TestKeepNoneSwitch:
    """P-35: the ``keep_none`` keyword selects *where* the params go.

    Default ``False`` = query-string semantics (httpx drops ``None``
    values, so they must not be signed) — behaviour identical to every
    vector above.  ``True`` = JSON-body semantics (a ``null`` is
    transmitted and the upstream folds ``str(None) == "None"`` into its
    digest), so the ``None`` must be counted.  Vectors computed offline
    against the deployed 0.2.9 implementation.
    """

    def test_keep_none_counts_none_as_the_string_none(self) -> None:
        with_none = signing.sign(
            "/api/v0.1/ci", SECRET, {"a": 1, "b": None}, keep_none=True
        )
        assert with_none == "e5e4eb0e7fc9deeacc750c4e434234edf1286295"
        # Exactly what upstream signs: the literal string "None".
        assert with_none == signing.sign(
            "/api/v0.1/ci", SECRET, {"a": 1, "b": "None"}
        )

    def test_default_still_drops_none(self) -> None:
        dropped = signing.sign("/api/v0.1/ci", SECRET, {"a": 1, "b": None})
        assert dropped == "caaac94cc5a95e2db50d6e19adda41e782e82210"
        assert dropped == signing.sign("/api/v0.1/ci", SECRET, {"a": 1})

    def test_keep_none_changes_the_digest(self) -> None:
        # The switch must not be cosmetic: same params, different digests.
        assert signing.sign(
            "/api/v0.1/ci", SECRET, {"note": None}, keep_none=True
        ) == "925b5cba55491e33f9a2ec3ddbf5ecd8db4bde7b"
        assert signing.sign("/api/v0.1/ci", SECRET, {"note": None}) == signing.sign(
            "/api/v0.1/ci", SECRET, {}
        )

    def test_keep_none_still_excludes_creds_and_containers(self) -> None:
        # keep_none only affects None handling; _key/_secret and
        # container values remain excluded either way.
        body = {"_key": "K", "_secret": "x", "tags": ["a"], "note": None, "ci_type": 7}
        assert signing.sign(
            "/api/v0.1/ci", SECRET, body, keep_none=True
        ) == signing.sign("/api/v0.1/ci", SECRET, {"note": None, "ci_type": 7}, keep_none=True)

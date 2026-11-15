"""Request signing for the VeOps CMDB (OneOps) API.

VeOps 2.6.2 authenticates service-to-service calls with a per-user
``key``/``secret`` pair (see ``users.key`` / ``users.secret`` in the
VeOps database, exposed on the VeOps *user management* page).  Every
request carries two **query** parameters:

* ``_key`` — the user's key (an identity, not a secret);
* ``_secret`` — ``sha1(path + secret + <sorted scalar values>)`` where
  ``path`` is the raw ``request.path`` (no query string) and the value
  suffix concatenates the ``str(v)`` of *every* scalar request parameter
  (query **and** form/JSON body), excluding ``_key``/``_secret``, with
  entries sorted by parameter name.

The upstream computes this over ``request.values`` — and its
``auth_required`` wrapper *rewrites* ``request.values`` to the parsed JSON
body when a JSON body is present (``request.values = request.json``),
otherwise it is the query string.  So the signed value set is:

* **read calls** (no body): the query scalar values;
* **write calls** (JSON body): the body's scalar values.

The credentials ride wherever ``request.values`` lives: as query parameters
for reads, and inside the JSON body for writes.  :func:`sign` is agnostic
to that — it just hashes ``path + secret + <sorted scalar values>`` for
whichever parameter set the caller passes in.

This module is *pure*: no I/O, no state.  The exact same :func:`sign`
is used by the production client and by the test stubs, so a regression
in the algorithm is caught by the stub rejecting the request.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

#: Query parameter that carries the user key.
KEY_PARAM = "_key"

#: Query parameter that carries the request signature.
SECRET_PARAM = "_secret"


def _scalar_params(
    params: Mapping[str, Any], *, keep_none: bool = False
) -> dict[str, Any]:
    """Keep only scalar (non-container) parameter values, minus the
    signing parameters themselves.

    Whether ``None`` may be dropped depends on **where the parameters go on
    the wire**, so it is an explicit switch rather than one global rule:

    * ``keep_none=False`` (default, **query string**) — httpx drops ``None``
      query values, the server never sees them, so they must not be signed.
    * ``keep_none=True`` (**JSON body**) — ``null`` *is* transmitted, and the
      upstream's ``auth_required`` rewrites ``request.values`` to the parsed
      body, after which ``lib/perm/auth.py`` folds ``str(None) == "None"``
      into its signature string (it excludes only ``_key``/``_secret`` and
      dict/list values).  A body-side ``None`` must therefore be sent *and*
      signed together; the two must never disagree.

    The previous unconditional drop was self-consistent only because the
    client also stripped ``None`` out of the outgoing body — which quietly
    threw away a caller's deliberate "clear this field" (register **P-35**;
    the T-A/T-B/T-C signing contrast in cmdb-audit ``out/36`` proves both
    directions).
    """
    out: dict[str, Any] = {}
    for name, value in params.items():
        if name in (KEY_PARAM, SECRET_PARAM):
            continue
        if value is None and not keep_none:
            continue
        if isinstance(value, (dict, list, tuple, set)):
            continue
        out[name] = value
    return out


def sign(
    path: str,
    secret: str,
    params: Mapping[str, Any],
    *,
    keep_none: bool = False,
) -> str:
    """Compute the ``_secret`` query value for one request.

    Args:
        path: The request path, e.g. ``/api/v0.1/ci_types`` (no query
            string — the upstream signs ``request.path``).
        secret: The user's VeOps ``secret`` (never the key, never a
            password).
        params: The scalar request values to sign — the query string for
            read calls, or the JSON body for write calls — excluding
            ``_key``/``_secret``.  Only scalar values are used;
            mapping/sequence values are skipped (the upstream's
            ``request.values`` projection excludes containers).
        keep_none: Pass ``True`` when ``params`` is a JSON **body** (a
            ``null`` is transmitted and counted by the upstream as the
            string ``"None"``); leave ``False`` for **query** parameters,
            which httpx omits when ``None``.  Getting this wrong in the
            ``True`` direction yields a 401; getting it wrong the other way
            yields a silent no-op.  See :func:`_scalar_params`.

    Returns:
        The lowercase hex SHA-1 digest, i.e.
        ``sha1(path + secret + "".join(str(v) for k, v in sorted(items)))``.
    """
    items = _scalar_params(params, keep_none=keep_none)
    parts = "".join(str(v) for _k, v in sorted(items.items()))
    return hashlib.sha1(f"{path}{secret}{parts}".encode()).hexdigest()


def signed_params(
    path: str,
    key: str,
    secret: str,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """``params`` plus the ``_key``/``_secret`` query parameters.

    The returned mapping is ready to be used as httpx ``params``: the
    signature is computed over the original scalar parameters (never
    over itself).
    """
    out: dict[str, Any] = {
        name: value for name, value in params.items() if value is not None
    }
    out[KEY_PARAM] = key
    out[SECRET_PARAM] = sign(path, secret, params)
    return out


__all__ = ["KEY_PARAM", "SECRET_PARAM", "sign", "signed_params"]

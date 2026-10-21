"""The HTTP front door: the bearer check and the health endpoint.

In HTTP mode, reaching the MCP endpoint means reaching the CalDAV
account — `MCP_AUTH_TOKEN` is the only check in front of it. So the interesting
cases here are not "does the happy path work" but the ways a request could slip
past: a missing header, a header that is not valid UTF-8, a prefix match.
"""
import asyncio

import pytest

import server as server_module

TOKEN = "s3cret-token"  # noqa: S105 — a fixture value, not a credential


async def _ok_app(scope, receive, send):
    await server_module._plain_response(send, 200, b"MCP-APP")


def call(app, path="/mcp", headers=(), scope_type="http"):
    """Drive an ASGI app once; return (status, body)."""
    captured = {}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
        elif message["type"] == "http.response.body":
            captured["body"] = message.get("body", b"")

    scope = {"type": scope_type, "path": path, "headers": [
        (k.lower().encode() if isinstance(k, str) else k,
         v.encode() if isinstance(v, str) else v)
        for k, v in headers
    ]}
    asyncio.run(app(scope, None, send))
    return captured.get("status"), captured.get("body")


@pytest.fixture
def guarded():
    """The production stack: healthz outside, bearer check inside."""
    return server_module.HealthzMiddleware(
        server_module.BearerAuthMiddleware(_ok_app, TOKEN))


@pytest.fixture
def unguarded():
    """What runs under MCP_ALLOW_UNAUTHENTICATED — the explicit opt-out, and
    unauthenticated. Without that variable the server refuses to start at all;
    see the startup guard below."""
    return server_module.HealthzMiddleware(_ok_app)


# ── the bearer check ─────────────────────────────────────────────────────────

def test_correct_token_passes(guarded):
    assert call(guarded, headers=[("authorization", f"Bearer {TOKEN}")]) == (200, b"MCP-APP")


REJECTED_HEADERS = {
    "no header at all": [],
    "empty value": [("authorization", "")],
    "wrong token": [("authorization", "Bearer wrong-token")],
    "missing scheme": [("authorization", TOKEN)],
    "wrong scheme": [("authorization", f"Basic {TOKEN}")],
    "lowercase scheme": [("authorization", f"bearer {TOKEN}")],
    # A prefix must not pass — compare_digest is on the whole value.
    "token prefix": [("authorization", f"Bearer {TOKEN[:-1]}")],
    "token with suffix": [("authorization", f"Bearer {TOKEN}x")],
    "leading space": [("authorization", f" Bearer {TOKEN}")],
    "trailing space": [("authorization", f"Bearer {TOKEN} ")],
}


@pytest.mark.parametrize("headers", REJECTED_HEADERS.values(), ids=list(REJECTED_HEADERS))
def test_rejected(guarded, headers):
    assert call(guarded, headers=headers) == (401, b"unauthorized")


def test_non_utf8_header_is_a_clean_401_not_a_traceback(guarded):
    """The header is compared as raw bytes for exactly this case.

    Decoding first would raise UnicodeDecodeError, and compare_digest on `str`
    rejects non-ASCII with a TypeError — either turns a request anyone can send
    into a 500 with a traceback.
    """
    assert call(guarded, headers=[(b"authorization", b"Bearer \xff\xfe\x00")]) == (
        401, b"unauthorized")


def test_non_http_scope_is_passed_through(guarded):
    """Lifespan and websocket scopes must not hit the header logic."""
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    stack = server_module.HealthzMiddleware(
        server_module.BearerAuthMiddleware(app, TOKEN))
    asyncio.run(stack({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


# ── the health endpoint ──────────────────────────────────────────────────────

def test_healthz_needs_no_token(guarded):
    assert call(guarded, path="/healthz") == (200, b"ok")


def test_healthz_exists_without_a_configured_token(unguarded):
    """An unauthenticated instance needs the probe too — deploy.sh polls it and
    would otherwise report a working deployment as a failure."""
    assert call(unguarded, path="/healthz") == (200, b"ok")


def test_healthz_ignores_a_bogus_token(guarded):
    assert call(guarded, path="/healthz",
                headers=[("authorization", "Bearer nonsense")]) == (200, b"ok")


def test_healthz_does_not_shadow_other_paths(guarded):
    """Only the exact path is the probe — /healthz-ish paths stay guarded."""
    for path in ("/healthzz", "/healthz/x", "/mcp/healthz"):
        assert call(guarded, path=path)[0] == 401, path


# ── the startup guard ────────────────────────────────────────────────────────
#
# The bearer check above only guards requests that reach a server which is
# already running. These pin the step before it: whether an HTTP run starts at
# all without a token, since an unauthenticated endpoint is full access to the
# calendars.

def test_a_token_starts_guarded():
    assert server_module._bearer_check_or_refuse(TOKEN, False) is True


def test_no_token_refuses_to_start():
    with pytest.raises(OSError) as excinfo:
        server_module._bearer_check_or_refuse("", False)
    # The message has to name the way out, or the refusal just blocks a deploy.
    assert "MCP_ALLOW_UNAUTHENTICATED" in str(excinfo.value)


def test_no_token_starts_unguarded_only_when_opted_in():
    assert server_module._bearer_check_or_refuse("", True) is False


def test_a_token_wins_over_the_opt_out():
    """The opt-out is about the missing token, not a switch that disables the
    check — a config carrying both must still be guarded."""
    assert server_module._bearer_check_or_refuse(TOKEN, True) is True


OPT_OUT_VALUES = {
    "1": True, "true": True, "TRUE": True, "yes": True, " yes ": True,
    "": False, "0": False, "false": False, "no": False, "maybe": False,
}


@pytest.mark.parametrize("value,expected", OPT_OUT_VALUES.items(), ids=list(OPT_OUT_VALUES))
def test_opt_out_parsing(monkeypatch, value, expected):
    """A variable set to "0" or "false" must read as off, and an unrecognised
    value must fall to the safe side rather than count as on because it is
    non-empty."""
    monkeypatch.setenv("MCP_ALLOW_UNAUTHENTICATED", value)
    assert server_module._env_flag("MCP_ALLOW_UNAUTHENTICATED") is expected


def test_opt_out_is_off_when_unset(monkeypatch):
    monkeypatch.delenv("MCP_ALLOW_UNAUTHENTICATED", raising=False)
    assert server_module._env_flag("MCP_ALLOW_UNAUTHENTICATED") is False

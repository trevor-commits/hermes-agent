"""Regression tests for the Anthropic OAuth PKCE flow.

Guards against re-introducing the bug where the PKCE ``code_verifier`` was
reused as the OAuth ``state`` parameter, leaking the verifier via the
authorization URL (browser history, Referer headers, auth-server logs) and
removing CSRF protection on the callback path.

History:
  - PR #1775 first fixed this on ``run_hermes_oauth_login()``.
  - PR #2647 (b17e5c10) added ``run_hermes_oauth_login_pure()`` and silently
    copy-pasted the pre-#1775 vulnerable pattern.
  - PR #3107 removed the old function, leaving only the regressed copy.
  - PR #10699 (issue #10693) fixed the regression on the surviving function.
"""

from __future__ import annotations

import io
import json
import pytest
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse


def _patch_oauth_flow(
    monkeypatch,
    *,
    callback_code: str,
    token_response: Dict[str, Any] | None = None,
    capture_token_request: Dict[str, Any] | None = None,
    capture_auth_url: Dict[str, str] | None = None,
) -> None:
    """Wire up monkeypatches that let ``run_hermes_oauth_login_pure()`` run
    end-to-end without touching a real browser, stdin, or HTTP endpoint.

    ``callback_code`` is the literal string the user would paste back into the
    terminal (``"<code>#<state>"`` format).
    ``capture_token_request`` and ``capture_auth_url`` are out-dict captures
    so the test can introspect what was sent to the auth URL and the token
    endpoint, respectively.
    """
    import urllib.request

    if token_response is None:
        token_response = {
            "access_token": "sk-ant-test-access",
            "refresh_token": "sk-ant-test-refresh",
            "expires_in": 3600,
        }

    def fake_open(url):
        if capture_auth_url is not None:
            capture_auth_url["url"] = url
        return True

    monkeypatch.setattr("webbrowser.open", fake_open)
    # The flow now gates webbrowser.open() behind a graphical-browser check so
    # it never launches a console browser (w3m/lynx) inside the terminal. Tests
    # run headless, so force the GUI path to True — the URL capture relies on
    # webbrowser.open() being invoked.
    monkeypatch.setattr(
        "hermes_cli.auth._can_open_graphical_browser", lambda: True
    )
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: callback_code)

    class _FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return self._body

    def fake_urlopen(req, *_a, **_kw):
        if capture_token_request is not None:
            capture_token_request["url"] = req.full_url
            capture_token_request["data"] = json.loads(req.data.decode())
            capture_token_request["headers"] = dict(req.headers)
        return _FakeResponse(json.dumps(token_response).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def test_authorization_url_state_is_not_pkce_verifier(monkeypatch, tmp_path):
    """The ``state`` parameter in the authorization URL must NOT equal the
    PKCE ``code_verifier``.

    Reusing the verifier as state leaks the verifier into browser history,
    Referer headers, and auth-server access logs — defeating RFC 7636.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    captured_url: Dict[str, str] = {}
    captured_token: Dict[str, Any] = {}
    _patch_oauth_flow(
        monkeypatch,
        # state echoed back unchanged so the CSRF guard passes
        callback_code="auth-code-from-anthropic#PLACEHOLDER",
        capture_auth_url=captured_url,
        capture_token_request=captured_token,
    )

    # Stub the callback parse: we need the state echoed back to match. To do
    # that without hardcoding the state value, override input() AFTER seeing
    # the auth URL.
    import builtins

    real_input_calls = {"count": 0}

    def fake_input(*_a, **_kw):
        real_input_calls["count"] += 1
        # First (and only) call is the "Authorization code:" prompt.
        url = captured_url.get("url", "")
        qs = parse_qs(urlparse(url).query)
        state = qs.get("state", [""])[0]
        return f"auth-code-from-anthropic#{state}"

    monkeypatch.setattr(builtins, "input", fake_input)

    from agent.anthropic_credentials import run_hermes_oauth_login_pure

    result = run_hermes_oauth_login_pure()
    assert result is not None, "OAuth flow should succeed with matching state"

    url = captured_url["url"]
    qs = parse_qs(urlparse(url).query)

    assert "state" in qs and qs["state"][0], "authorization URL must include state"
    assert "code_challenge" in qs, "authorization URL must include code_challenge"

    state_in_url = qs["state"][0]
    verifier_sent = captured_token["data"]["code_verifier"]

    # The whole point: state and verifier must be independent values.
    assert state_in_url != verifier_sent, (
        "PKCE code_verifier was reused as OAuth state — regression of #10693 / "
        "#1775. The verifier is supposed to be a secret known only to the "
        "client; placing it in the authorization URL leaks it via browser "
        "history, Referer headers, and auth-server logs."
    )

    # And the verifier MUST NOT appear anywhere in the URL.
    assert verifier_sent not in url, (
        "PKCE verifier leaked into authorization URL — regression of #10693"
    )


def test_login_token_exchange_uses_platform_claude_host(monkeypatch, tmp_path):
    """The login token exchange must hit ``platform.claude.com`` first.

    Anthropic migrated the OAuth token endpoint to ``platform.claude.com``;
    ``console.anthropic.com`` now 404s, so a hardcoded console host makes a
    fresh login impossible (issue #45250 / #49821). The refresh path already
    iterates the new host first — the login path must do the same.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    captured_token: Dict[str, Any] = {}
    captured_url: Dict[str, str] = {}
    _patch_oauth_flow(
        monkeypatch,
        callback_code="placeholder",
        capture_token_request=captured_token,
        capture_auth_url=captured_url,
    )

    import builtins

    def fake_input(*_a, **_kw):
        qs = parse_qs(urlparse(captured_url.get("url", "")).query)
        state = qs.get("state", [""])[0]
        return f"auth-code#{state}"

    monkeypatch.setattr(builtins, "input", fake_input)

    from agent.anthropic_credentials import run_hermes_oauth_login_pure

    result = run_hermes_oauth_login_pure()

    assert result is not None, "login should succeed against the live host"
    assert captured_token["url"] == "https://platform.claude.com/v1/oauth/token", (
        "login token exchange must target platform.claude.com first, not the "
        "dead console.anthropic.com host (regression of #45250 / #49821)"
    )




def test_callback_state_mismatch_aborts(monkeypatch, tmp_path, caplog):
    """If the state returned in the callback does not match the one we sent
    in the authorization URL, the flow must abort before exchanging the code.

    Without this check, an attacker who tricks the user into pasting a
    crafted ``<code>#<state>`` string can complete the token exchange — the
    CSRF protection that ``state`` is supposed to provide (RFC 6749 §10.12)
    would be absent.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    captured_token: Dict[str, Any] = {}
    _patch_oauth_flow(
        monkeypatch,
        callback_code="attacker-code#attacker-state-does-not-match",
        capture_token_request=captured_token,
    )

    from agent.anthropic_credentials import run_hermes_oauth_login_pure

    result = run_hermes_oauth_login_pure()

    assert result is None, "mismatched state must abort the flow"
    assert "url" not in captured_token, (
        "token exchange must NOT happen when state mismatches"
    )


def test_token_endpoint_failure_reports_every_attempt(monkeypatch, tmp_path):
    """A token-endpoint failure must name each endpoint's outcome.

    ``_post_oauth_token`` tries platform.claude.com first, then the legacy
    console.anthropic.com fallback. When the PRIMARY returns a meaningful
    error (e.g. HTTP 400 ``invalid_grant`` for a revoked refresh token) and
    the FALLBACK 404s, raising only the last exception surfaced the fallback's
    bare "HTTP Error 404: Not Found" and buried the diagnostic 400. Callers
    (``hermes auth refresh``) then show a symptom-free error. Regression:
    2026-09-09 — refresh of a revoked grant reported 404; the actual first
    endpoint response was ``invalid_grant: Refresh token not found or
    invalid``.
    """
    import urllib.error
    import urllib.request

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    seen_urls: list[str] = []

    def fake_urlopen(req, timeout=None):  # noqa: ARG001 - signature mirrors urllib
        seen_urls.append(req.full_url)
        if "platform.claude.com" in req.full_url:
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {},
                io.BytesIO(b'{"error":"invalid_grant","error_description":"Refresh token not found or invalid"}'),
            )
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    from agent.anthropic_credentials import refresh_anthropic_oauth_pure

    with pytest.raises(RuntimeError) as excinfo:
        refresh_anthropic_oauth_pure("«redacted:sk-…»")

    message = str(excinfo.value)
    assert "platform.claude.com" in message, f"primary host missing from error: {message!r}"
    assert "invalid_grant" in message, f"primary error body missing from error: {message!r}"
    assert "404" in message, f"fallback outcome missing from error: {message!r}"
    assert seen_urls == [
        "https://platform.claude.com/v1/oauth/token",
        "https://console.anthropic.com/v1/oauth/token",
    ], f"both endpoints must be attempted in order: {seen_urls!r}"


def test_login_eof_names_the_non_interactive_cause(monkeypatch, tmp_path, capsys):
    """EOF at the authorization-code prompt must explain the non-interactive stdin.

    ``run_hermes_oauth_login_pure`` reads the pasted code with ``input()``.
    Under a non-interactive stdin (agent-driven background process), it hits
    EOF immediately and returned None indistinguishably from Ctrl-C, so the
    CLI printed only "Anthropic OAuth login did not return credentials." with
    no hint. Regression: 2026-09-09 — a background pty-less launch died at the
    prompt, the code pasted afterwards was bound to a dead PKCE verifier, and
    the user had to authorize a second time.
    """
    import builtins

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def raising_input(*_a, **_kw):
        raise EOFError

    monkeypatch.setattr(builtins, "input", raising_input)

    from agent.anthropic_credentials import run_hermes_oauth_login_pure

    result = run_hermes_oauth_login_pure()

    assert result is None, "EOF must abort the login without credentials"
    out = capsys.readouterr().out
    assert "No interactive terminal is available" in out, f"EOF cause missing: {out!r}"
    assert "hermes auth add anthropic --type oauth" in out, f"remedy missing: {out!r}"

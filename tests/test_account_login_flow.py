# -*- coding: utf-8 -*-
from __future__ import annotations

import itertools
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import account_login_flow


class FakePage:
    def __init__(self, state):
        self.state = state
        self.wait = SimpleNamespace(doc_loaded=lambda: None)

    def get(self, url):
        self.state["url"] = url

    def run_js(self, _script):
        return {"invalid": False, "rate": False, "cf": False, "url": self.state.get("url", "")}


def test_password_login_returns_sso_without_logging_secrets():
    state = {"phase": "email", "typed": [], "logs": []}
    page = FakePage(state)
    email_element = object()
    password_element = object()

    def candidates(kind):
        if kind == "email" and state["phase"] == "email":
            return [email_element]
        if kind == "password" and state["phase"] == "password":
            return [password_element]
        return []

    def type_element(element, value):
        state["typed"].append((element, value))
        return True

    def click_action(*_args, **_kwargs):
        if state["phase"] == "email":
            state["phase"] = "password-choice"
        elif state["phase"] == "password-choice" and any(
            "password" in str(value).lower() for value in (_args[0] if _args else ())
        ):
            state["phase"] = "password"
        return "continue"

    fake_browser = SimpleNamespace(active_page=lambda: page, refresh_active_page=lambda: page)
    fake_flow = SimpleNamespace(
        _dismiss_cookie_consent=lambda **_kwargs: None,
        _native_input_candidates=candidates,
        _native_type_element=type_element,
        _native_click_action=click_action,
        _try_sync_turnstile=lambda **_kwargs: None,
        wait_for_sso_cookie=lambda **_kwargs: "-private-sso-token",
    )
    ticks = itertools.count(0, 1)
    with patch.dict(sys.modules, {"browser_session": fake_browser, "register_flow": fake_flow}), patch.object(
        account_login_flow.time, "monotonic", side_effect=lambda: next(ticks)
    ), patch.object(account_login_flow, "_sleep", return_value=None):
        sso = account_login_flow.login_and_extract_sso(
            "person@example.test",
            "private-password",
            log_callback=state["logs"].append,
        )

    assert sso == "private-sso-token"
    assert (email_element, "person@example.test") in state["typed"]
    assert (password_element, "private-password") in state["typed"]
    log_text = "\n".join(state["logs"])
    assert "private-password" not in log_text
    assert "private-sso-token" not in log_text
    assert "Selected password sign-in" in log_text


def test_email_verification_page_fails_with_specific_safe_diagnostic():
    email = "person@example.test"
    password = "private-password"
    state = {"logs": [], "url": "https://accounts.x.ai/verify"}

    class VerificationPage(FakePage):
        def run_js(self, _script):
            return {
                "invalid": False,
                "rate": False,
                "cf": False,
                "verification": True,
                "url": self.state["url"],
                "title": f"Verify {email}",
                "ready": "complete",
                "inputs": [{"type": "text", "name": "verification-code", "autocomplete": "one-time-code"}],
                "actions": [f"Send code to {email}"],
            }

    page = VerificationPage(state)
    fake_browser = SimpleNamespace(active_page=lambda: page, refresh_active_page=lambda: page)
    fake_flow = SimpleNamespace(
        _dismiss_cookie_consent=lambda **_kwargs: None,
        _native_input_candidates=lambda _kind: [],
        _native_click_action=lambda *_args, **_kwargs: "",
        _try_sync_turnstile=lambda **_kwargs: None,
    )
    ticks = itertools.count(0, 1)
    with patch.dict(sys.modules, {"browser_session": fake_browser, "register_flow": fake_flow}), patch.object(
        account_login_flow.time, "monotonic", side_effect=lambda: next(ticks)
    ), patch.object(account_login_flow, "_sleep", return_value=None):
        with pytest.raises(account_login_flow.AccountLoginError, match="email verification code"):
            account_login_flow.login_and_extract_sso(
                email,
                password,
                log_callback=state["logs"].append,
            )

    log_text = "\n".join(state["logs"])
    assert email not in log_text
    assert password not in log_text
    assert "verification-code" in log_text


def test_email_form_is_retried_then_fails_with_specific_reason():
    state = {"typed": [], "logs": []}
    page = FakePage(state)
    email_element = object()

    def click_action(keywords, *_args, **_kwargs):
        if any("password" in str(value).lower() for value in keywords):
            return ""
        return "continue"

    fake_browser = SimpleNamespace(active_page=lambda: page, refresh_active_page=lambda: page)
    fake_flow = SimpleNamespace(
        _dismiss_cookie_consent=lambda **_kwargs: None,
        _native_input_candidates=lambda kind: [email_element] if kind == "email" else [],
        _native_type_element=lambda element, value: state["typed"].append((element, value)) or True,
        _native_click_action=click_action,
        _try_sync_turnstile=lambda **_kwargs: None,
    )
    ticks = itertools.count(0, 1)
    with patch.dict(sys.modules, {"browser_session": fake_browser, "register_flow": fake_flow}), patch.object(
        account_login_flow.time, "monotonic", side_effect=lambda: next(ticks)
    ), patch.object(account_login_flow, "_sleep", return_value=None):
        with pytest.raises(account_login_flow.AccountLoginError, match="did not advance past the email form"):
            account_login_flow.login_and_extract_sso(
                "person@example.test",
                "private-password",
                log_callback=state["logs"].append,
            )

    assert len(state["typed"]) == 3
    assert any("resubmitted (3/3)" in line for line in state["logs"])


def test_standard_form_submit_then_react_password_button_are_used():
    state = {"phase": "email", "request_submits": 0, "js_clicks": 0, "native_clicks": 0}

    class SubmitPage(FakePage):
        def run_js(self, script):
            if "form.requestSubmit" in script:
                state["request_submits"] += 1
                state["phase"] = "password"
                return True
            if "const labels = new Set" in script:
                state["js_clicks"] += 1
                return "Login"
            return {
                "invalid": False,
                "rate": False,
                "cf": False,
                "verification": False,
                "url": self.state.get("url", ""),
            }

    page = SubmitPage(state)
    email_element = object()
    password_element = object()

    def candidates(kind):
        if kind == "email" and state["phase"] == "email":
            return [email_element]
        if kind == "password" and state["phase"] == "password":
            return [password_element]
        return []

    def native_click(*_args, **_kwargs):
        state["native_clicks"] += 1
        return "unexpected"

    fake_browser = SimpleNamespace(active_page=lambda: page, refresh_active_page=lambda: page)
    fake_flow = SimpleNamespace(
        _dismiss_cookie_consent=lambda **_kwargs: "",
        _native_input_candidates=candidates,
        _native_type_element=lambda _element, _value: True,
        _native_click_action=native_click,
        _try_sync_turnstile=lambda **_kwargs: None,
        wait_for_sso_cookie=lambda **_kwargs: "-private-sso-token",
    )
    ticks = itertools.count(0, 1)
    with patch.dict(sys.modules, {"browser_session": fake_browser, "register_flow": fake_flow}), patch.object(
        account_login_flow.time, "monotonic", side_effect=lambda: next(ticks)
    ), patch.object(account_login_flow, "_sleep", return_value=None):
        sso = account_login_flow.login_and_extract_sso(
            "person@example.test",
            "private-password",
        )

    assert sso == "private-sso-token"
    assert state["request_submits"] == 1
    assert state["js_clicks"] == 1
    assert state["native_clicks"] == 0


if __name__ == "__main__":
    test_password_login_returns_sso_without_logging_secrets()
    test_email_verification_page_fails_with_specific_safe_diagnostic()
    test_email_form_is_retried_then_fails_with_specific_reason()
    test_standard_form_submit_then_react_password_button_are_used()
    print("OK account login flow")

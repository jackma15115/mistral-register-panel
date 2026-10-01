"""Browser password-login flow used by imported account jobs."""

from __future__ import annotations

import json
import re
import time

from sso_utils import normalize_sso_token


SIGNIN_URL = "https://accounts.x.ai/sign-in?redirect=grok-com"


class AccountLoginError(RuntimeError):
    pass


def _sleep(seconds: float, cancel_callback=None) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if cancel_callback and cancel_callback():
            raise AccountLoginError("login cancelled")
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def _page_flags(page) -> dict:
    try:
        value = page.run_js(
            r"""
const body = ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ').trim();
const lower = body.toLowerCase();
const shown = (node) => {
  if (!node) return false;
  const style = getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0
    && rect.width > 0 && rect.height > 0;
};
const visible = (node) => shown(node) && !node.disabled;
const clean = (value, limit = 64) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, limit);
const inputNodes = Array.from(document.querySelectorAll('input, textarea')).filter(shown).slice(0, 8);
const inputs = inputNodes.map((node) => ({
  type: clean(node.getAttribute('type') || node.tagName.toLowerCase(), 24),
  name: clean(node.getAttribute('name'), 40),
  autocomplete: clean(node.getAttribute('autocomplete'), 40),
  testid: clean(node.getAttribute('data-testid'), 40),
  valueLength: String(node.value || '').length,
  trackedLength: node._valueTracker && typeof node._valueTracker.getValue === 'function'
    ? String(node._valueTracker.getValue() || '').length : null,
  valid: node.validity ? !!node.validity.valid : null,
  validation: clean(node.validationMessage, 100),
  focused: document.activeElement === node,
  disabled: !!node.disabled,
  readOnly: !!node.readOnly,
  ariaInvalid: clean(node.getAttribute('aria-invalid'), 16)
}));
const actionNodes = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]')).filter(shown);
const actions = actionNodes
  .map((node) => clean(node.innerText || node.textContent || node.value || node.getAttribute('aria-label'), 64))
  .filter(Boolean).slice(0, 8);
const nextNode = actionNodes.find((node) => /^(next|continue|sign in|log in)$/i.test(
  clean(node.innerText || node.textContent || node.value || node.getAttribute('aria-label'), 64)
));
const nextState = nextNode ? {
  label: clean(nextNode.innerText || nextNode.textContent || nextNode.value || nextNode.getAttribute('aria-label'), 64),
  disabled: !!nextNode.disabled,
  ariaDisabled: clean(nextNode.getAttribute('aria-disabled'), 16),
  type: clean(nextNode.getAttribute('type'), 20)
} : null;
const emailNode = inputNodes.find((node) => (node.type || '').toLowerCase() === 'email'
  || /email/i.test((node.name || '') + ' ' + (node.autocomplete || '')));
const formNode = emailNode && emailNode.form;
const formState = formNode ? {
  method: clean(formNode.method, 16),
  action: (() => { try { const u = new URL(formNode.action); return u.origin + u.pathname; } catch (_) { return ''; } })(),
  noValidate: !!formNode.noValidate
} : null;
const feedback = Array.from(document.querySelectorAll(
  '[role="alert"], [aria-live], [data-testid*="error" i], [class*="error" i], [class*="invalid" i]'
)).filter(shown).map((node) => clean(node.innerText || node.textContent, 160)).filter(Boolean);
const network = performance.getEntriesByType('resource').filter((entry) =>
  ['fetch', 'xmlhttprequest'].includes(String(entry.initiatorType || '').toLowerCase())
).slice(-6).map((entry) => ({
  url: (() => { try { const u = new URL(entry.name); return u.origin + u.pathname; } catch (_) { return ''; } })(),
  status: Number(entry.responseStatus || 0),
  duration: Math.round(Number(entry.duration || 0))
}));
const cfSelector = 'iframe[src*="turnstile"], iframe[src*="challenge"], .cf-turnstile, [data-sitekey], input[name="cf-turnstile-response"], #challenge-running, [id*="cf-chl"]';
const cfText = lower.includes('verify you are human') || lower.includes('performing security verification')
  || lower.includes('checking your browser') || lower.includes('security verification')
  || lower.includes('just a moment') || lower.includes('challenges.cloudflare.com');
const codeInput = inputs.some((item) => item.autocomplete === 'one-time-code'
  || /(^|[-_])(otp|code|verification)([-_]|$)/i.test(item.name + ' ' + item.testid));
const verificationText = lower.includes('enter the code') || lower.includes('verification code')
  || lower.includes('one-time code') || lower.includes('check your email')
  || body.includes('验证码') || body.includes('检查你的邮箱') || body.includes('查看你的邮箱');
return {
  invalid: lower.includes('invalid credentials') || lower.includes('incorrect password')
    || lower.includes('wrong password') || lower.includes('invalid password')
    || lower.includes('account not found') || lower.includes('email or password is incorrect')
    || body.includes('密码错误') || body.includes('账号或密码错误') || body.includes('找不到账号'),
  rate: lower.includes('too many attempts') || lower.includes('try again later')
    || body.includes('尝试次数过多') || body.includes('稍后再试'),
  cf: !!document.querySelector(cfSelector) || cfText,
  verification: codeInput || verificationText,
  url: String(location.origin || '') + String(location.pathname || ''),
  title: clean(document.title, 100),
  ready: String(document.readyState || ''),
  inputs,
  actions,
  nextState,
  formState,
  feedback: Array.from(new Set(feedback)).slice(0, 4),
  network,
  cookie: actions.some((text) => /^(accept all cookies|reject all|cookies settings)$/i.test(text))
};
            """
        )
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")


def _safe_diagnostic_value(value: object, *secrets: object, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    for secret in secrets:
        raw = str(secret or "")
        if raw:
            text = text.replace(raw, "[redacted]")
    text = _EMAIL_RE.sub("[redacted-email]", text)
    return text[:limit]


def _page_diagnostic(flags: dict, stage: str, email: str, password: str) -> str:
    input_labels = []
    for item in flags.get("inputs") or []:
        if not isinstance(item, dict):
            continue
        parts = []
        for key in ("type", "name", "autocomplete", "testid"):
            value = _safe_diagnostic_value(item.get(key), email, password, limit=40)
            if value:
                parts.append(f"{key}={value}")
        if parts:
            input_labels.append("{" + ",".join(parts) + "}")
    actions = [
        _safe_diagnostic_value(value, email, password, limit=64)
        for value in (flags.get("actions") or [])[:8]
    ]
    input_state = []
    for item in flags.get("inputs") or []:
        if not isinstance(item, dict):
            continue
        kind = " ".join(
            str(item.get(key) or "") for key in ("type", "name", "autocomplete", "testid")
        ).lower()
        if "email" not in kind:
            continue
        state = {}
        for key in (
            "valueLength",
            "trackedLength",
            "valid",
            "validation",
            "focused",
            "disabled",
            "readOnly",
            "ariaInvalid",
        ):
            value = item.get(key)
            if value in (None, ""):
                continue
            state[key] = (
                _safe_diagnostic_value(value, email, password, limit=100)
                if isinstance(value, str)
                else value
            )
        input_state.append(state)
    next_state = flags.get("nextState") if isinstance(flags.get("nextState"), dict) else {}
    form_state = flags.get("formState") if isinstance(flags.get("formState"), dict) else {}
    network = []
    for item in (flags.get("network") or [])[-6:]:
        if not isinstance(item, dict):
            continue
        network.append(
            {
                "url": _safe_diagnostic_value(item.get("url"), email, password, limit=160),
                "status": int(item.get("status") or 0),
                "duration": int(item.get("duration") or 0),
            }
        )
    feedback = [
        _safe_diagnostic_value(value, email, password, limit=160)
        for value in (flags.get("feedback") or [])[:4]
    ]
    state_flags = [name for name in ("invalid", "rate", "cf", "verification") if flags.get(name)]
    url = _safe_diagnostic_value(flags.get("url"), email, password, limit=180) or "unknown"
    title = _safe_diagnostic_value(flags.get("title"), email, password, limit=100) or "--"
    return (
        f"[Debug] Login state: stage={stage} url={url} title={title} "
        f"ready={_safe_diagnostic_value(flags.get('ready'), limit=20) or '--'} "
        f"inputs={input_labels or ['none']} input_state={input_state or ['none']} "
        f"actions={actions or ['none']} next={next_state or 'none'} "
        f"form={form_state or 'none'} network={network or ['none']} "
        f"feedback={feedback or ['none']} cookie={bool(flags.get('cookie'))} "
        f"flags={state_flags or ['none']}"
    )[:1200]


def _read_sso_cookie(page) -> str:
    try:
        cookies = page.cookies(all_domains=True, all_info=True) or []
    except Exception:
        return ""
    fallback = ""
    for item in cookies:
        if isinstance(item, dict):
            name = str(item.get("name") or "")
            value = str(item.get("value") or "").strip()
        else:
            name = str(getattr(item, "name", "") or "")
            value = str(getattr(item, "value", "") or "").strip()
        if name == "sso" and value:
            return normalize_sso_token(value)
        if name == "sso-rw" and value:
            fallback = normalize_sso_token(value)
    return fallback


def _request_form_submit(page, selector: str) -> bool:
    try:
        return bool(
            page.run_js(
                f"""
const input = document.querySelector({selector!r});
const form = input && input.form;
if (!form) return false;
const submitter = Array.from(form.querySelectorAll('button, input[type="submit"]')).find((node) =>
  !node.disabled && node.getAttribute('aria-disabled') !== 'true'
  && (node.type === 'submit' || node.tagName === 'BUTTON')
);
form.requestSubmit(submitter || undefined);
return true;
"""
            )
        )
    except Exception:
        return False


def _press_enter_submit(element) -> bool:
    try:
        element.input("\n", clear=False, by_js=False)
        return True
    except Exception:
        return False


def _js_click_action(page, labels: tuple[str, ...]) -> str:
    encoded = json.dumps([str(value).strip().lower() for value in labels], ensure_ascii=True)
    try:
        result = page.run_js(
            fr"""
const labels = new Set({encoded});
const shown = (node) => {{
  const style = getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden'
    && Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
}};
const candidates = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]'));
const target = candidates.find((node) => {{
  const text = String(node.innerText || node.textContent || node.value || node.getAttribute('aria-label') || '')
    .replace(/\s+/g, ' ').trim().toLowerCase();
  return labels.has(text) && shown(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
}});
if (!target) return '';
const label = String(target.innerText || target.textContent || target.value || target.getAttribute('aria-label') || '')
  .replace(/\s+/g, ' ').trim();
target.click();
return label;
"""
        )
        return str(result or "").strip()
    except Exception:
        return ""


def _timeout_error(flags: dict, *, email_submitted: bool, password_submitted: bool) -> str:
    if flags.get("cf"):
        return "timed out on the xAI/Cloudflare verification challenge"
    if flags.get("verification"):
        return "xAI requires an email verification code; password login cannot continue automatically"
    visible_inputs = flags.get("inputs") or []
    has_email = any(
        isinstance(item, dict)
        and (
            str(item.get("type") or "").lower() == "email"
            or "email" in str(item.get("name") or "").lower()
            or "email" in str(item.get("autocomplete") or "").lower()
        )
        for item in visible_inputs
    )
    if password_submitted:
        return "timed out waiting for an SSO cookie after password submission"
    if email_submitted and has_email:
        return "xAI did not advance past the email form after repeated submissions"
    if email_submitted:
        return "timed out waiting for the xAI password form after email submission"
    return "timed out waiting for the xAI sign-in form"


def login_and_extract_sso(
    email: str,
    password: str,
    *,
    log_callback=None,
    cancel_callback=None,
    timeout: int = 120,
) -> str:
    """Sign in with email/password and return the resulting SSO cookie."""
    import browser_session as browser
    import register_flow as flow

    page = browser.active_page()
    if page is None:
        raise AccountLoginError("browser session is not active")
    deadline = time.monotonic() + max(30, int(timeout or 120))
    if log_callback:
        log_callback("[*] Opening xAI sign-in page")
    page.get(SIGNIN_URL)
    try:
        page.wait.doc_loaded()
    except Exception:
        pass
    try:
        flow._dismiss_cookie_consent(log_callback=log_callback)
    except Exception:
        pass

    email_submitted = False
    password_submitted = False
    email_submit_attempts = 0
    last_action = 0.0
    last_email_submit = 0.0
    last_cf_attempt = 0.0
    last_password_choice = 0.0
    last_diagnostic = -10.0
    last_flags: dict = {}
    verification_since = None

    while time.monotonic() < deadline:
        if cancel_callback and cancel_callback():
            raise AccountLoginError("login cancelled")
        browser.refresh_active_page()
        page = browser.active_page()
        if page is None:
            _sleep(0.4, cancel_callback)
            continue

        flags = _page_flags(page)
        last_flags = flags
        if flags.get("invalid"):
            raise AccountLoginError("xAI rejected the account credentials")
        if flags.get("rate"):
            raise AccountLoginError("xAI rate limited the login attempt")

        sso = _read_sso_cookie(page)
        if sso:
            if log_callback:
                log_callback("[*] SSO cookie detected")
            return sso

        if flags.get("cookie"):
            try:
                dismissed = flow._dismiss_cookie_consent(log_callback=log_callback)
            except Exception:
                dismissed = ""
            if dismissed:
                _sleep(0.4, cancel_callback)
                continue

        now = time.monotonic()
        password_fields = flow._native_input_candidates("password")
        email_fields = flow._native_input_candidates("email")
        if flags.get("cf"):
            stage = "verification-challenge"
        elif flags.get("verification"):
            stage = "verification-code"
        elif password_submitted:
            stage = "after-password"
        elif password_fields:
            stage = "password-form"
        elif email_submitted:
            stage = "after-email"
        elif email_fields:
            stage = "email-form"
        else:
            stage = "sign-in-method"
        if log_callback and now - last_diagnostic >= 10:
            last_diagnostic = now
            log_callback(_page_diagnostic(flags, stage, email, password))

        if flags.get("cf") and now - last_cf_attempt >= 8:
            last_cf_attempt = now
            if log_callback:
                log_callback("[*] Waiting for the login verification challenge")
            try:
                flow._try_sync_turnstile(
                    log_callback=log_callback,
                    cancel_callback=cancel_callback,
                    reason="password login challenge",
                )
            except Exception:
                pass

        if password_fields and not password_submitted and now - last_action >= 0.8:
            if not flow._native_type_element(password_fields[0], password):
                raise AccountLoginError("could not fill the xAI password field")
            clicked = _js_click_action(page, ("login", "sign in", "log in", "continue", "next", "登录", "继续"))
            submitted = bool(clicked)
            submit_method = "js-click" if submitted else ""
            if not submitted:
                submitted = _request_form_submit(
                    page,
                    'input[type="password"], input[name="password"], input[autocomplete="current-password"]',
                )
                submit_method = "request-submit-fallback" if submitted else ""
            if not submitted:
                clicked = flow._native_click_action(
                    ("sign in", "signin", "log in", "login", "continue", "next", "登录", "继续"),
                    ("google", "apple", "x.com", "twitter", "back", "返回", "forgot"),
                )
                submitted = bool(clicked)
                submit_method = "native-click-fallback" if submitted else ""
            if not submitted:
                raise AccountLoginError("could not submit the xAI password form")
            password_submitted = True
            last_action = now
            if log_callback:
                log_callback(f"[Debug] Password submit method: {submit_method}")
                log_callback("[*] Password submitted; waiting for SSO")
            _sleep(1.0, cancel_callback)
            submitted_flags = _page_flags(browser.active_page()) if browser.active_page() else {}
            if log_callback:
                log_callback(_page_diagnostic(submitted_flags, "password-submit", email, password))
            if submitted_flags.get("invalid"):
                raise AccountLoginError("xAI rejected the account credentials")
            if submitted_flags.get("rate"):
                raise AccountLoginError("xAI rate limited the login attempt")
            remaining = max(8, int(deadline - time.monotonic()))
            try:
                sso = flow.wait_for_sso_cookie(
                    timeout=remaining,
                    log_callback=log_callback,
                    cancel_callback=cancel_callback,
                )
            except Exception as exc:
                final_flags = _page_flags(browser.active_page()) if browser.active_page() else {}
                if log_callback:
                    log_callback(_page_diagnostic(final_flags, "after-password", email, password))
                if final_flags.get("invalid"):
                    raise AccountLoginError("xAI rejected the account credentials") from exc
                if final_flags.get("rate"):
                    raise AccountLoginError("xAI rate limited the login attempt") from exc
                raise AccountLoginError(
                    _timeout_error(final_flags, email_submitted=True, password_submitted=True)
                ) from exc
            sso = normalize_sso_token(sso)
            if not sso:
                raise AccountLoginError("login completed without an SSO cookie")
            return sso

        if (
            email_fields
            and not password_submitted
            and email_submitted
            and email_submit_attempts >= 3
            and now - last_email_submit >= 8
        ):
            if log_callback:
                log_callback(_page_diagnostic(flags, "email-form-stuck", email, password))
            raise AccountLoginError("xAI did not advance past the email form after repeated submissions")

        email_retry_due = (
            not email_submitted
            or (email_submit_attempts < 3 and now - last_email_submit >= 8)
        )
        if email_fields and not password_submitted and email_retry_due and now - last_action >= 0.8:
            if not flow._native_type_element(email_fields[0], email):
                raise AccountLoginError("could not fill the xAI email field")
            submit_method = ""
            if email_submit_attempts == 0:
                submitted = _request_form_submit(
                    page,
                    'input[type="email"], input[name="email"], input[autocomplete="email"], input[autocomplete="username"]',
                )
                submit_method = "request-submit" if submitted else ""
            elif email_submit_attempts == 1:
                clicked = _js_click_action(
                    page,
                    ("continue", "next", "sign in", "log in", "login", "继续", "下一步", "登录"),
                )
                submitted = bool(clicked)
                submit_method = "js-click" if submitted else ""
            else:
                submitted = _press_enter_submit(email_fields[0])
                submit_method = "enter-key" if submitted else ""
            if not submitted:
                clicked = flow._native_click_action(
                    ("continue", "next", "sign in", "signin", "log in", "login", "继续", "下一步", "登录"),
                    ("google", "apple", "x.com", "twitter", "back", "返回", "use email"),
                )
                submitted = bool(clicked)
                submit_method = "native-click-fallback" if submitted else ""
            if not submitted:
                raise AccountLoginError("could not submit the xAI email form")
            email_submitted = True
            email_submit_attempts += 1
            last_email_submit = now
            last_action = now
            if log_callback:
                log_callback(f"[Debug] Email submit method: {submit_method}")
                if email_submit_attempts == 1:
                    log_callback("[*] Email submitted; waiting for password page")
                else:
                    log_callback(f"[*] Email form still visible; resubmitted ({email_submit_attempts}/3)")
            _sleep(0.8, cancel_callback)
            continue

        if email_submitted and not password_fields and now - last_password_choice >= 4:
            last_password_choice = now
            clicked = flow._native_click_action(
                (
                    "use password",
                    "sign in with password",
                    "continue with password",
                    "password instead",
                    "password",
                    "使用密码",
                    "密码登录",
                ),
                (
                    "forgot",
                    "reset",
                    "change",
                    "create",
                    "show password",
                    "hide password",
                    "忘记密码",
                    "重置密码",
                ),
            )
            if clicked:
                last_action = now
                if log_callback:
                    log_callback("[*] Selected password sign-in")
                _sleep(0.6, cancel_callback)
                continue

        if flags.get("verification") and not password_fields:
            if verification_since is None:
                verification_since = now
            if now - verification_since >= 6:
                if log_callback:
                    log_callback(_page_diagnostic(flags, "verification-code", email, password))
                raise AccountLoginError(
                    "xAI requires an email verification code; password login cannot continue automatically"
                )
            _sleep(0.4, cancel_callback)
            continue
        verification_since = None

        if not email_fields and not password_fields and now - last_action >= 2:
            clicked = flow._native_click_action(
                ("sign in with email", "continue with email", "use email", "email", "邮箱登录", "使用邮箱"),
                ("google", "apple", "x.com", "twitter"),
            )
            if clicked:
                last_action = now
                if log_callback:
                    log_callback("[*] Selected email sign-in")

        _sleep(0.4, cancel_callback)

    if log_callback:
        log_callback(_page_diagnostic(last_flags, "timeout", email, password))
    raise AccountLoginError(
        _timeout_error(
            last_flags,
            email_submitted=email_submitted,
            password_submitted=password_submitted,
        )
    )

# -*- coding: utf-8 -*-
"""Mistral 注册页流程：打开注册、填邮箱/验证码/密码、提取 API 密钥。"""
from __future__ import annotations

import logging
import os
import random
import re
import secrets
import string
import threading
import time
from typing import Any, Dict, Optional, Tuple

from playwright._impl._errors import TargetClosedError as PageDisconnectedError

from browser_session import (
    active_browser,
    active_page,
    browser,
    page,
    refresh_active_page,
    restart_browser,
    set_browser_session,
    start_browser,
    stop_browser,
)

logger = logging.getLogger(__name__)

SIGNUP_URL = "https://admin.mistral.ai/organization/api-keys"

_deps: Dict[str, Any] = {}
_flow_tls = threading.local()


def configure(**kwargs):
    _deps.update(kwargs)


def _AccountRetryNeeded(msg=""):
    cls = _deps.get("AccountRetryNeeded", Exception)
    return cls(msg)


def _AccountDomainRejected(msg=""):
    cls = _deps.get("EmailDomainRejected", Exception)
    return cls(msg)


def raise_if_cancelled(cancel_callback=None):
    fn = _deps.get("raise_if_cancelled")
    if fn:
        return fn(cancel_callback)


def sleep_with_cancel(seconds, cancel_callback=None):
    fn = _deps.get("sleep_with_cancel")
    if fn:
        return fn(seconds, cancel_callback)
    time.sleep(max(seconds, 0))


def _generate_strong_password() -> str:
    """生成满足 Mistral 复杂度要求的强密码。"""
    chars = string.ascii_letters + string.digits
    rnd = "".join(secrets.choice(chars) for _ in range(8))
    return f"Mistral#{rnd}!2a"


def _generate_profile_names() -> Tuple[str, str]:
    first_names = [
        "Alex", "Chris", "Jordan", "Taylor", "Morgan", "Sam", "Robin", "Pat",
        "James", "David", "Michael", "Daniel", "Matthew", "Andrew", "Ryan",
    ]
    last_names = [
        "Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis",
        "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    ]
    return random.choice(first_names), random.choice(last_names)


def build_profile() -> Dict[str, str]:
    first, last = _generate_profile_names()
    password = _generate_strong_password()
    return {
        "given_name": first,
        "family_name": last,
        "password": password,
    }


def _dismiss_cookie_consent(log_callback=None):
    """尝试关闭通用 Cookie 横幅，避免遮挡元素。"""
    refresh_active_page()
    if not page:
        return ""
    try:
        dismissed = page.run_js(r"""
        const oneTrustBtn = document.querySelector(
            '#onetrust-reject-all-handler, #onetrust-accept-btn-handler, #accept-recommended-btn-handler'
        );
        if (oneTrustBtn) { oneTrustBtn.click(); return 'OneTrust'; }
        const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
        const exactLabels = new Set([
            'reject all', 'reject all cookies', 'accept all', 'accept all cookies',
            'accept', 'agree', '同意', '全部接受', '接受', 'close'
        ]);
        for (const b of btns) {
            const t = (b.innerText || b.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
            if (exactLabels.has(t)) {
                b.click(); return 'generic:' + t;
            }
        }
        return '';
        """)
        if dismissed and log_callback:
            log_callback(f"[*] 已关闭 Cookie 横幅: {dismissed}")
        return str(dismissed or "")
    except Exception:
        return ""


def detect_email_domain_rejection(body_text: str = "") -> bool:
    """检测 Ory Kratos 或页面返回的邮箱域名拒绝/无效错误。"""
    low = (body_text or "").lower()
    return (
        "email invalid" in low
        or "traits.email" in low
        or "disposable email" in low
        or "invalid email domain" in low
        or "email address is not allowed" in low
    )


def raise_if_email_domain_rejected(body_text: str, email: str = ""):
    if detect_email_domain_rejection(body_text):
        fn = _deps.get("on_email_domain_rejected")
        if fn:
            fn(email, "Mistral rejected email domain")
        raise _AccountDomainRejected(f"Mistral 拒绝邮箱域名: {email}")


def has_profile_form(log_callback=None) -> bool:
    """兼容旧接口。"""
    return False


def getTurnstileToken(log_callback=None, cancel_callback=None) -> str:
    """兼容旧接口（Mistral 不使用 Turnstile）。"""
    return ""


def click_email_signup_button(log_callback=None, cancel_callback=None) -> bool:
    """兼容旧接口。"""
    return True


def authorize_device_in_browser(*args, **kwargs) -> bool:
    """兼容旧接口。"""
    return True


def get_current_account_password() -> str:
    return getattr(_flow_tls, "password", "")


def get_current_account_profile() -> Dict[str, str]:
    return getattr(_flow_tls, "profile", {})


def get_current_account_api_key() -> str:
    return getattr(_flow_tls, "api_key", "")


def open_signup_page(log_callback=None, cancel_callback=None):
    """打开 Mistral 注册入口并等待登录/注册界面加载。"""
    raise_if_cancelled(cancel_callback)
    if active_browser() is None:
        start_browser(log_callback=log_callback)
        if log_callback:
            log_callback("[*] 浏览器已启动")

    browser_obj = active_browser()
    if browser_obj is None:
        start_browser(log_callback=log_callback)
        browser_obj = active_browser()

    try:
        tabs = browser_obj.get_tabs() if browser_obj is not None else []
        page_obj = tabs[-1] if tabs else browser_obj.new_tab()
    except Exception:
        page_obj = browser_obj.new_tab()

    set_browser_session(browser_obj, page_obj)

    if log_callback:
        log_callback(f"[*] 正在打开注册页: {SIGNUP_URL}")
    page_obj.get(SIGNUP_URL)
    try:
        page_obj.wait.doc_loaded()
    except Exception:
        pass

    raw_page = page_obj.raw_page
    # 等待重定向至 Ory Kratos 或展示邮箱输入框
    email_found = False
    for _ in range(30):
        raise_if_cancelled(cancel_callback)
        try:
            _dismiss_cookie_consent(log_callback=log_callback)
            if raw_page.locator('input[name="email"], input[type="email"]').count() > 0:
                email_found = True
                break
        except Exception:
            pass
        sleep_with_cancel(0.8, cancel_callback)

    current_url = getattr(page_obj, "url", "")
    if log_callback:
        log_callback(f"[*] 页面加载完成: URL={current_url}")

    if not email_found:
        # 如果已登录直接在 admin 页面，检查是否有 New key 按钮
        try:
            if raw_page.locator('button:has-text("New key")').count() > 0:
                if log_callback:
                    log_callback("[*] 检测到已处于登录状态")
                return
        except Exception:
            pass
        raise Exception(f"未找到邮箱输入框，当前页面 URL: {current_url}")


def fill_email_and_submit(timeout=20, log_callback=None, cancel_callback=None) -> Tuple[str, str]:
    """生成并填入邮箱，提交后输入密码和姓名，点击 Signup。返回 (email, dev_token)。"""
    raise_if_cancelled(cancel_callback)
    email, dev_token = _deps['get_email_and_token']()
    if not email or not dev_token:
        raise Exception("获取邮箱失败")

    first_name, last_name = _generate_profile_names()
    password = _generate_strong_password()

    _flow_tls.email = email
    _flow_tls.dev_token = dev_token
    _flow_tls.password = password
    _flow_tls.first_name = first_name
    _flow_tls.last_name = last_name
    _flow_tls.profile = {
        "given_name": first_name,
        "family_name": last_name,
        "password": password,
    }

    if log_callback:
        log_callback(f"[*] 已生成邮箱: {email}")

    refresh_active_page()
    raw_page = active_page().raw_page

    # 1. 填入邮箱
    email_inp = raw_page.locator('input[name="email"], input[type="email"]').first
    email_inp.fill(email)
    sleep_with_cancel(0.5, cancel_callback)

    # 点击 Continue 或按回车提交邮箱
    continue_btn = raw_page.locator('button:has-text("Continue"), button[type="submit"]').first
    try:
        if continue_btn.is_visible():
            continue_btn.click()
        else:
            email_inp.press("Enter")
    except Exception:
        email_inp.press("Enter")

    if log_callback:
        log_callback("[*] 已提交邮箱，等待表单展开...")

    # 2. 等待密码字段展开或检测域名拒绝错误
    deadline = time.time() + timeout
    pwd_ready = False
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            body_text = raw_page.locator("body").inner_text()
            raise_if_email_domain_rejected(body_text, email)
            # 检查是否有 toast 提示
            toasts = raw_page.locator('[data-sonner-toast]').all()
            for t in toasts:
                try:
                    txt = t.inner_text()
                    raise_if_email_domain_rejected(txt, email)
                except Exception:
                    pass

            if raw_page.locator('input[name="password"]').count() > 0:
                pwd_ready = True
                break
        except _AccountDomainRejected:
            raise
        except Exception:
            pass
        sleep_with_cancel(0.8, cancel_callback)

    if not pwd_ready:
        body_text = ""
        try:
            body_text = raw_page.locator("body").inner_text()[:300]
        except Exception:
            pass
        raise_if_email_domain_rejected(body_text, email)
        raise _AccountRetryNeeded(f"提交邮箱后未展开密码输入框: {body_text}")

    # 3. 填入密码与姓名
    if log_callback:
        log_callback("[*] 正在填写密码与注册信息...")
    raw_page.locator('input[name="password"]').first.fill(password)
    sleep_with_cancel(0.3, cancel_callback)

    first_inp = raw_page.locator('input[name="firstName"]')
    if first_inp.count() > 0:
        first_inp.first.fill(first_name)
        sleep_with_cancel(0.2, cancel_callback)

    last_inp = raw_page.locator('input[name="lastName"]')
    if last_inp.count() > 0:
        last_inp.first.fill(last_name)
        sleep_with_cancel(0.2, cancel_callback)

    # 4. 点击注册按钮
    signup_btn = raw_page.locator('button:has-text("Signup"), button:has-text("Sign up"), button[type="submit"]').last
    try:
        signup_btn.click()
    except Exception:
        if last_inp.count() > 0:
            last_inp.first.press("Enter")
        else:
            raw_page.locator('input[name="password"]').first.press("Enter")

    sleep_with_cancel(1.5, cancel_callback)
    # 若表单未消失，再次尝试按 Enter 提交
    try:
        if raw_page.locator('input[name="password"]').count() > 0:
            if last_inp.count() > 0:
                last_inp.first.press("Enter")
            else:
                raw_page.locator('input[name="password"]').first.press("Enter")
    except Exception:
        pass

    on_acc = _deps.get("on_email_accepted")
    if on_acc:
        on_acc(email)

    return email, dev_token


def fill_code_and_submit(email: str, dev_token: str, timeout: int = 150, log_callback=None, cancel_callback=None) -> str:
    """等待 OTP 输入框出现，从邮箱获取 6 位数字验证码并提交。"""
    raise_if_cancelled(cancel_callback)
    refresh_active_page()
    raw_page = active_page().raw_page

    # 等待验证码输入框展示
    if log_callback:
        log_callback("[*] 等待验证码输入框...")
    otp_ready = False
    otp_selector = 'input[data-input-otp="true"], input[autocomplete="one-time-code"], input[name="code"]'
    for _ in range(25):
        raise_if_cancelled(cancel_callback)
        try:
            body_text = raw_page.locator("body").inner_text()
            raise_if_email_domain_rejected(body_text, email)
            if raw_page.locator(otp_selector).count() > 0:
                otp_ready = True
                break
        except _AccountDomainRejected:
            raise
        except Exception:
            pass
        sleep_with_cancel(0.8, cancel_callback)

    if not otp_ready:
        raise _AccountRetryNeeded("未检测到验证码输入框")

    # 轮询邮箱获取验证码
    if log_callback:
        log_callback(f"[*] 正在拉取邮箱验证码: {email}")
    get_code_fn = _deps['get_oai_code']
    code = get_code_fn(
        dev_token,
        email,
        timeout=timeout,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )
    if not code:
        raise Exception(f"未收到验证码: {email}")

    code = str(code).strip()
    if log_callback:
        log_callback(f"[*] 填入验证码: {code}")

    otp_inp = raw_page.locator(otp_selector).first
    otp_inp.focus()
    try:
        otp_inp.press_sequentially(code, delay=50)
    except Exception:
        otp_inp.fill(code)
    sleep_with_cancel(0.8, cancel_callback)
    otp_inp.press("Enter")

    # 某些界面有专门的提交按钮
    try:
        verify_btn = raw_page.locator('button:has-text("Verify"), button:has-text("Submit"), button[type="submit"]')
        if verify_btn.count() > 0 and verify_btn.first.is_visible():
            verify_btn.first.click()
    except Exception:
        pass

    # 等待跳转完成
    if log_callback:
        log_callback("[*] 正在等待注册验证完成并跳转...")
    redirected = False
    for _ in range(35):
        raise_if_cancelled(cancel_callback)
        current = raw_page.url
        if "admin.mistral.ai" in current or "console.mistral.ai" in current:
            redirected = True
            break
        try:
            btn = raw_page.locator('button:has-text("Verify"), button:has-text("Submit"), button[type="submit"]')
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
        except Exception:
            pass
        sleep_with_cancel(1.0, cancel_callback)

    if not redirected and log_callback:
        log_callback(f"[*] 验证码提交后 URL: {raw_page.url}")

    return code


def create_and_extract_api_key(timeout: int = 45, log_callback=None, cancel_callback=None) -> str:
    """在 Mistral API Key 管理页面创建并提取 API Key（mstrl_...）。"""
    raise_if_cancelled(cancel_callback)
    refresh_active_page()
    raw_page = active_page().raw_page

    # 确保当前在 API keys 页面
    if "organization/api-keys" not in raw_page.url:
        if log_callback:
            log_callback("[*] 正在进入 API Key 管理页面...")
        raw_page.goto("https://admin.mistral.ai/organization/api-keys", timeout=30000)
        sleep_with_cancel(3.0, cancel_callback)

    captured_keys: list[str] = []

    def on_resp(resp):
        try:
            url = resp.url.lower()
            if ("api-key" in url or "keys" in url or "billing" in url) and resp.status in (200, 201):
                try:
                    data = resp.json()
                    if isinstance(data, dict):
                        k = data.get("key") or data.get("apiKey")
                        if k and isinstance(k, str) and k.startswith("mstrl_"):
                            captured_keys.append(k)
                except Exception:
                    pass
                if not captured_keys:
                    txt = resp.text()
                    keys = re.findall(r'\"key\"\s*:\s*\"(mstrl_[^\"]+)\"', txt)
                    if keys:
                        captured_keys.extend(keys)
        except Exception:
            pass

    raw_page.on("response", on_resp)

    # 等待 "+ New key" 按钮出现
    if log_callback:
        log_callback("[*] 寻找 '+ New key' 创建按钮...")
    new_key_btn = None
    for _ in range(25):
        raise_if_cancelled(cancel_callback)
        try:
            btn = raw_page.locator('button:has-text("New key")')
            if btn.count() > 0 and btn.first.is_visible():
                new_key_btn = btn.first
                break
        except Exception:
            pass
        sleep_with_cancel(0.8, cancel_callback)

    if not new_key_btn:
        raise Exception(f"未找到 '+ New key' 按钮，当前 URL: {raw_page.url}")

    new_key_btn.click()
    sleep_with_cancel(1.5, cancel_callback)

    # 处理创建 Key 对话框
    dialog = raw_page.locator('div[role="dialog"]')
    for _ in range(10):
        if dialog.count() > 0 and dialog.first.is_visible():
            break
        sleep_with_cancel(0.5, cancel_callback)

    # 输入 Key 名称（可选）
    try:
        name_inp = raw_page.locator('div[role="dialog"] input[name="name"], div[role="dialog"] input[placeholder*="name" i]')
        if name_inp.count() > 0:
            name_inp.first.fill("auto_key")
            sleep_with_cancel(0.4, cancel_callback)
    except Exception:
        pass

    # 检查是否有 Workspace 下拉选择
    try:
        ws_btn = raw_page.locator('div[role="dialog"] button:has-text("Select workspace")')
        if ws_btn.count() > 0 and ws_btn.first.is_visible():
            if log_callback:
                log_callback("[*] 选择 Workspace...")
            ws_btn.first.click()
            sleep_with_cancel(0.8, cancel_callback)
            opt = raw_page.locator('div:has-text("Default Workspace")')
            if opt.count() > 0:
                opt.last.click()
            else:
                raw_page.locator('[role="option"]').first.click()
            sleep_with_cancel(0.5, cancel_callback)
    except Exception:
        pass

    # 点击模态框内的提交按钮 "New key"
    if log_callback:
        log_callback("[*] 提交创建 API Key...")
    clicked = False
    btn_candidates = raw_page.locator('div[role="dialog"] button:has-text("New key")')
    for b in btn_candidates.all():
        try:
            if b.is_visible():
                b.click()
                clicked = True
                break
        except Exception:
            pass
    if not clicked:
        submits = raw_page.locator('div[role="dialog"] button[type="submit"]').all()
        for b in submits:
            try:
                if b.is_visible():
                    b.click()
                    clicked = True
                    break
            except Exception:
                pass
    if not clicked and btn_candidates.count() > 0:
        btn_candidates.first.click(force=True)
    sleep_with_cancel(2.0, cancel_callback)

    # 等待并提取 Key
    deadline = time.time() + timeout
    api_key = ""
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if captured_keys:
            api_key = captured_keys[0]
            break
        try:
            # 从模态框中提取
            if dialog.count() > 0:
                dtext = dialog.first.inner_text()
                m = re.search(r"\b(mstrl_[A-Za-z0-9_]+)\b", dtext)
                if m:
                    api_key = m.group(1)
                    break
            # 从只读输入框提取
            ro_inp = raw_page.locator('div[role="dialog"] input[readonly], div[role="dialog"] input')
            for inp in ro_inp.all():
                try:
                    val = inp.input_value()
                    if val and val.startswith("mstrl_"):
                        api_key = val
                        break
                except Exception:
                    pass
            if api_key:
                break
        except Exception:
            pass
        sleep_with_cancel(0.8, cancel_callback)

    if not api_key:
        # 页面兜底查找
        try:
            body_text = raw_page.locator("body").inner_text()
            m = re.search(r"\b(mstrl_[A-Za-z0-9_]+)\b", body_text)
            if m:
                api_key = m.group(1)
        except Exception:
            pass

    if not api_key:
        raise Exception("未能成功提取 Mistral API Key")

    _flow_tls.api_key = api_key
    if log_callback:
        masked = f"{api_key[:8]}...{api_key[-4:]}"
        log_callback(f"[+] 成功获取 Mistral API Key: {masked}")

    return api_key


def fill_profile_and_submit(log_callback=None, cancel_callback=None) -> Dict[str, str]:
    """兼容旧接口：返回当前账号信息。"""
    return getattr(_flow_tls, "profile", build_profile())


def wait_for_sso_cookie(log_callback=None, cancel_callback=None, email=None, password=None) -> str:
    """兼容旧接口：创建并返回 API Key。"""
    return create_and_extract_api_key(log_callback=log_callback, cancel_callback=cancel_callback)

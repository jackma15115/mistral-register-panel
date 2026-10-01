"""TI Temp Mail provider (``POST /mailbox`` and ``GET /messages``)."""

from __future__ import annotations

import random
import re
import time
from typing import Any, Callable, List, Optional, Tuple
from urllib.parse import quote, urlparse

from email_providers.common import extract_verification_code

HttpGet = Callable[..., Any]
HttpPost = Callable[..., Any]

DEFAULT_BASE_URL = "https://anymail.77669876.xyz"


def normalize_base(base_url: str = "") -> str:
    """Normalize a docs or endpoint URL to the TI API origin."""
    raw = str(base_url or "").strip()
    if not raw:
        return DEFAULT_BASE_URL
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc:
        return DEFAULT_BASE_URL
    return f"{parsed.scheme or 'https'}://{parsed.netloc}".rstrip("/")


def normalize_mode(mode: str = "") -> str:
    value = str(mode or "maindomain").strip().lower().replace("_", "").replace("-", "")
    if value in {"subdomain", "sub", "wildcard"}:
        return "subdomain"
    return "maindomain"


def _domains(value: object) -> List[str]:
    domains: List[str] = []
    seen = set()
    for part in re.split(r"[,;，；\s]+", str(value or "")):
        domain = part.strip().lower().lstrip("@").strip(".")
        if domain and domain not in seen:
            seen.add(domain)
            domains.append(domain)
    return domains


def _headers(token: str = "", *, content_type: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {}
    value = str(token or "").strip()
    if value:
        # The service expects the raw token rather than a Bearer value.
        headers["Authorization"] = value
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _json(response: Any, action: str) -> Any:
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        detail = str(getattr(response, "text", "") or "")[:300]
        raise RuntimeError(f"TI Temp Mail {action} failed HTTP {status}: {detail}")
    try:
        return response.json()
    except Exception as exc:
        detail = str(getattr(response, "text", "") or "")[:300]
        raise RuntimeError(f"TI Temp Mail {action} returned non-JSON: {detail}") from exc


def create_mailbox(
    http_post: HttpPost,
    base_url: str = "",
    api_key: str = "",
    *,
    domains: Optional[List[str]] = None,
    domain: str = "",
    mailbox_mode: str = "maindomain",
) -> Tuple[str, str]:
    """Create an inbox and return ``(address, mailbox_token)``."""
    base = normalize_base(base_url)
    mode = normalize_mode(mailbox_mode)
    pool = _domains(domain) or _domains("\n".join(domains or []))
    selected = random.choice(pool) if pool else ""
    payload = {"type": mode}
    if selected:
        payload["domain"] = selected
    response = http_post(
        f"{base}/mailbox",
        json=payload,
        headers=_headers(api_key, content_type=True),
        timeout=30,
        proxies={},
    )
    data = _json(response, "create mailbox")
    if not isinstance(data, dict):
        raise RuntimeError(
            f"Unexpected TI Temp Mail create response: {type(data).__name__}"
        )
    token = str(data.get("token") or "").strip()
    address = str(data.get("mailbox") or data.get("email") or "").strip()
    if not token or not address or "@" not in address:
        raise RuntimeError("Unexpected TI Temp Mail create response: missing mailbox or token")
    return address, token


def fetch_messages(
    http_get: HttpGet,
    base_url: str,
    mailbox_token: str,
    email: str = "",
) -> List[dict]:
    token = str(mailbox_token or "").strip()
    if not token:
        raise ValueError("TI Temp Mail mailbox token missing")
    base = normalize_base(base_url)
    headers = _headers(token)
    response = http_get(
        f"{base}/messages",
        headers=headers,
        timeout=30,
        proxies={},
    )
    data = _json(response, "list messages")
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise RuntimeError("Unexpected TI Temp Mail messages response: missing messages list")

    rows: List[dict] = []
    for item in data["messages"][:20]:
        if not isinstance(item, dict):
            continue
        message_id = str(item.get("_id") or item.get("id") or "")
        row = {
            "id": message_id,
            "subject": str(item.get("subject") or ""),
            "from": str(item.get("from") or ""),
            "to": str(item.get("to") or email or ""),
            "text": str(item.get("bodyPreview") or ""),
            "html": "",
            "content": str(item.get("bodyPreview") or ""),
        }
        if message_id:
            try:
                detail_response = http_get(
                    f"{base}/messages/{quote(message_id, safe='')}",
                    headers=headers,
                    timeout=30,
                    proxies={},
                )
                detail = _json(detail_response, "get message detail")
                if isinstance(detail, dict):
                    row["subject"] = str(detail.get("subject") or row["subject"])
                    row["from"] = str(detail.get("from") or row["from"])
                    row["text"] = str(detail.get("bodyPreview") or row["text"])
                    row["html"] = str(detail.get("bodyHtml") or "")
                    row["content"] = row["text"] or row["html"]
                else:
                    row["_detail_error"] = (
                        f"Unexpected detail response: {type(detail).__name__}"
                    )
            except Exception as exc:
                row["_detail_error"] = f"{type(exc).__name__}: {exc}"[:240]
        rows.append(row)
    return rows


def wait_for_code(
    http_get: HttpGet,
    base_url: str,
    mailbox_token: str,
    email: str = "",
    *,
    timeout: int = 180,
    poll_interval: float = 3,
    raise_if_cancelled: Callable[[Optional[Callable[[], bool]]], None],
    sleep_with_cancel: Callable[[float, Optional[Callable[[], bool]]], None],
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
    resend_callback: Optional[Callable[[], None]] = None,
) -> str:
    """Poll TI inbox messages and emit token-free progress diagnostics."""
    del resend_callback
    base = normalize_base(base_url)
    started = time.time()
    deadline = started + max(1, int(timeout))
    attempt = 0
    last_count = -1
    last_error = ""
    last_detail_error = ""
    next_log_at = started

    def emit(message: str) -> None:
        if log_callback:
            log_callback(f"[TI Temp Mail] {message}")

    emit(f"开始收信：address={email} base={base} 超时={int(timeout)}秒 mailbox_token=set")
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        attempt += 1
        request_started = time.time()
        log_due = attempt == 1 or request_started >= next_log_at
        if log_due:
            emit(f"轮询 #{attempt} 请求中：GET {base}/messages")
        try:
            messages = fetch_messages(http_get, base, mailbox_token, email)
            now = time.time()
            detail_errors = [
                str(item.get("_detail_error") or "")
                for item in messages
                if isinstance(item, dict) and item.get("_detail_error")
            ]
            detail_error = detail_errors[0][:160] if detail_errors else ""
            changed = (
                len(messages) != last_count
                or bool(last_error)
                or detail_error != last_detail_error
                or now >= next_log_at
            )
            if log_due or changed:
                status = (
                    f"轮询 #{attempt}：耗时={now - started:.0f}秒 "
                    f"请求耗时={now - request_started:.1f}秒 邮件={len(messages)}"
                )
                if last_error:
                    status += "，接口已恢复"
                if last_detail_error and not detail_error:
                    status += "，详情接口已恢复"
                if detail_error:
                    status += f"，详情失败={detail_error}"
                emit(status)
                next_log_at = now + 5
            last_count = len(messages)
            last_error = ""
            last_detail_error = detail_error

            for item in messages:
                body = "\n".join(
                    str(item.get(key) or "")
                    for key in ("subject", "text", "html", "content", "from")
                )
                code = extract_verification_code(body, str(item.get("subject") or ""))
                if code:
                    emit(
                        f"找到验证码：轮询={attempt} 耗时={time.time() - started:.0f}秒 "
                        f"message_id={item.get('id') or '?'}"
                    )
                    return code
            if messages and (log_due or changed):
                subjects = ", ".join(
                    str(item.get("subject") or "<no subject>")
                    .replace("\r", " ")
                    .replace("\n", " ")[:80]
                    for item in messages[:3]
                )
                emit(f"收到邮件但未识别验证码：subjects={subjects or '<empty>'}")
        except Exception as exc:
            now = time.time()
            error = f"{type(exc).__name__}: {exc}"[:240]
            if error != last_error or now >= next_log_at:
                emit(
                    f"轮询 #{attempt} 异常：耗时={now - started:.0f}秒 "
                    f"请求耗时={now - request_started:.1f}秒 {error}"
                )
                next_log_at = now + 5
            last_error = error
        sleep_with_cancel(max(0.4, float(poll_interval)), cancel_callback)

    message = (
        f"收信超时：耗时={time.time() - started:.0f}秒 轮询={attempt} "
        f"最后邮件数={max(0, last_count)} 最后错误={last_error or '<none>'}"
    )
    emit(message)
    raise RuntimeError(f"TI Temp Mail {message}")

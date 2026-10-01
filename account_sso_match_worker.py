#!/usr/bin/env python3
"""Resolve pasted SSO values to imported accounts through the OAuth/CPA flow."""

from __future__ import annotations

import argparse
import signal
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from secure_files import atomic_write_json, atomic_write_text
from sso_utils import normalize_sso_token
from webui.account_login_store import (
    attach_sso_by_email,
    parse_sso_values,
    private_accounts,
)
from webui.security_utils import redact_log_line


ROOT = Path(__file__).resolve().parent
STOP_EVENT = threading.Event()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(message: object) -> None:
    print(redact_log_line(str(message or "")), flush=True)


def _safe_error(exc: object, *secrets: object) -> str:
    text = str(exc or "")
    for secret in secrets:
        value = str(secret or "")
        if value:
            text = text.replace(value, "[redacted]")
    return redact_log_line(text)[:240] or type(exc).__name__


def _install_signal_handlers() -> None:
    def _stop(_signum, _frame):
        STOP_EVENT.set()

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _stop)


def _read_private_input(path: Path) -> list[str]:
    try:
        return parse_sso_values(path.read_text(encoding="utf-8"))
    finally:
        path.unlink(missing_ok=True)


def _write_account_file(runtime, account: dict, sso: str) -> None:
    sso = normalize_sso_token(sso)
    path = Path(runtime.account_file_for_email(account["email"]))
    atomic_write_text(path, f"{account['email']}----{account['password']}----{sso}\n")


def process_one_sso(sso: str, runtime, accounts_by_email: dict[str, dict], index: int) -> dict:
    sso = normalize_sso_token(sso)
    if STOP_EVENT.is_set():
        return {"status": "cancelled"}

    resolved: dict[str, object] = {}
    try:
        proxy = runtime.pick_proxy_for_worker(index, 0)
        runtime.set_thread_proxy(proxy)

        def _on_token(_token: dict, auth_record: dict) -> bool:
            email = str(auth_record.get("email") or "").strip().lower()
            resolved["email"] = email
            account = accounts_by_email.get(email)
            if account is None:
                return False
            resolved["account"] = account
            try:
                _write_account_file(runtime, account, sso)
            except Exception as exc:
                resolved["write_error"] = exc
                raise
            return True

        cpa_ok = bool(
            runtime.add_sso_to_cpa(
                sso,
                email="",
                log_callback=_log,
                token_callback=_on_token,
                force_exchange=True,
                retain_failed_sso=False,
            )
        )
        if resolved.get("write_error") is not None:
            raise RuntimeError("could not write the canonical account file")
        account = resolved.get("account")
        if not isinstance(account, dict):
            if resolved.get("email"):
                _log("[account-sso-match] resolved email has no imported account")
                return {"status": "unmatched"}
            _log("[account-sso-match] SSO could not be exchanged and was discarded")
            return {"status": "unusable"}

        attached = attach_sso_by_email(
            account["email"],
            sso,
            cpa_ok=cpa_ok,
            last_error="" if cpa_ok else "CPA/Grok2API conversion failed; SSO retained",
        )
        if attached is None:
            return {"status": "unmatched"}
        _log(
            "[account-sso-match] matched imported account; "
            + ("CPA/Grok2API written" if cpa_ok else "SSO retained without CPA output")
        )
        return {"status": "success" if cpa_ok else "sso_only"}
    except Exception as exc:
        error = _safe_error(exc, sso, resolved.get("email"))
        _log(f"[account-sso-match] failed: {error}")
        _log(_safe_error(traceback.format_exc(), sso, resolved.get("email")))
        return {"status": "cancelled" if STOP_EVENT.is_set() else "failed", "error": error}
    finally:
        try:
            runtime.clear_thread_proxy()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Match SSO values to imported accounts")
    parser.add_argument("--input-file", required=True, help="owner-only temporary SSO file")
    parser.add_argument("--report-file", required=True, help="count-only JSON report")
    parser.add_argument("--log-file", default="", help="display-only log filename")
    args = parser.parse_args(argv)
    _install_signal_handlers()

    started_at = _utc_now()
    input_path = Path(args.input_file).resolve()
    report_path = Path(args.report_file).resolve()
    try:
        sso_values = _read_private_input(input_path)
        accounts = private_accounts()
        accounts_by_email = {item["email"].lower(): item for item in accounts}
        if not accounts_by_email:
            raise RuntimeError("no imported accounts are available for SSO matching")

        import grok_register_ttk as runtime

        runtime.load_config()
        runtime._wire_runtime_modules()
        runtime.load_proxy_pool()
        runtime.config["cpa_auto_add"] = True
    except Exception as exc:
        input_path.unlink(missing_ok=True)
        error = _safe_error(exc)
        _log(f"[account-sso-match] startup failed: {error}")
        atomic_write_json(
            report_path,
            {
                "ok": False,
                "job_kind": "sso_match",
                "started_at": started_at,
                "finished_at": _utc_now(),
                "input_count": 0,
                "matched_count": 0,
                "unusable_count": 0,
                "unmatched_count": 0,
                "cpa_success_count": 0,
                "sso_only_count": 0,
                "failure_count": 0,
                "cancelled_count": 0,
                "fatal_error": error,
                "log": Path(args.log_file).name,
            },
        )
        return 1

    _log(f"[account-sso-match] start inputs={len(sso_values)}")
    results = []
    for index, sso in enumerate(sso_values, 1):
        if STOP_EVENT.is_set():
            results.extend({"status": "cancelled"} for _ in sso_values[index - 1 :])
            break
        results.append(process_one_sso(sso, runtime, accounts_by_email, index))

    report = {
        "ok": True,
        "job_kind": "sso_match",
        "started_at": started_at,
        "finished_at": _utc_now(),
        "input_count": len(sso_values),
        "matched_count": sum(
            1 for item in results if item.get("status") in {"success", "sso_only"}
        ),
        "unusable_count": sum(1 for item in results if item.get("status") == "unusable"),
        "unmatched_count": sum(1 for item in results if item.get("status") == "unmatched"),
        "cpa_success_count": sum(1 for item in results if item.get("status") == "success"),
        "sso_only_count": sum(1 for item in results if item.get("status") == "sso_only"),
        "failure_count": sum(1 for item in results if item.get("status") == "failed"),
        "cancelled_count": sum(1 for item in results if item.get("status") == "cancelled"),
        "log": Path(args.log_file).name,
    }
    atomic_write_json(report_path, report)
    _log(
        f"[account-sso-match] finished matched={report['matched_count']} "
        f"unusable={report['unusable_count']} unmatched={report['unmatched_count']} "
        f"failed={report['failure_count']}"
    )
    return 0 if not report["failure_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

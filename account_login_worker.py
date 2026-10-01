#!/usr/bin/env python3
"""Background browser-login worker for imported xAI accounts."""

from __future__ import annotations

import argparse
import json
import signal
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from secure_files import atomic_write_json, atomic_write_text
from sso_utils import normalize_sso_token
from webui.account_login_store import private_accounts, update_account
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


def _safe_traceback(*secrets: object) -> str:
    text = traceback.format_exc()
    for secret in secrets:
        value = str(secret or "")
        if value:
            text = text.replace(value, "[redacted]")
    return redact_log_line(text)


def _install_signal_handlers() -> None:
    def _stop(_signum, _frame):
        STOP_EVENT.set()

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _stop)


def _write_account_file(runtime, email: str, password: str, sso: str) -> Path:
    sso = normalize_sso_token(sso)
    path = Path(runtime.account_file_for_email(email))
    atomic_write_text(path, f"{email}----{password}----{sso}\n")
    return path


def process_one_account(record: dict, runtime, *, worker_index: int, extract_cpa: bool) -> dict:
    from account_login_flow import login_and_extract_sso
    from browser_session import start_browser, stop_browser

    account_id = record["id"]
    email = record["email"]
    password = record["password"]
    sso = normalize_sso_token(record.get("sso"))
    browser_started = False

    if STOP_EVENT.is_set():
        update_account(account_id, status="cancelled", last_error="login job stopped")
        return {"id": account_id, "status": "cancelled"}

    update_account(account_id, status="running", last_error="")
    _log(f"[account-login W{worker_index}] starting {email}")
    try:
        proxy = runtime.pick_proxy_for_worker(worker_index, 0)
        runtime.set_thread_proxy(proxy)

        if not sso:
            try:
                start_browser(log_callback=_log)
                browser_started = True
            except Exception as exc:
                try:
                    runtime.record_proxy_boot_failure(proxy, exc)
                except Exception:
                    pass
                raise
            sso = normalize_sso_token(
                login_and_extract_sso(
                    email,
                    password,
                    log_callback=_log,
                    cancel_callback=STOP_EVENT.is_set,
                    timeout=120,
                )
            )
            _write_account_file(runtime, email, password, sso)
            update_account(account_id, sso=sso, last_login_at=_utc_now())
            _log(f"[account-login W{worker_index}] SSO extracted for {email}")

        if STOP_EVENT.is_set():
            update_account(
                account_id,
                status="cancelled",
                sso=sso,
                last_error="login job stopped after SSO extraction",
            )
            return {"id": account_id, "status": "cancelled", "has_sso": bool(sso)}

        if extract_cpa:
            cpa_ok = bool(runtime.add_sso_to_cpa(sso, email=email, log_callback=_log))
            if cpa_ok:
                update_account(account_id, status="success", sso=sso, cpa_ok=True, last_error="")
                _log(f"[account-login W{worker_index}] CPA/Grok2API written for {email}")
                return {"id": account_id, "status": "success", "has_sso": True, "cpa_ok": True}
            update_account(
                account_id,
                status="sso_only",
                sso=sso,
                cpa_ok=False,
                last_error="CPA/Grok2API conversion failed; SSO retained",
            )
            return {"id": account_id, "status": "sso_only", "has_sso": True, "cpa_ok": False}

        update_account(account_id, status="sso_only", sso=sso, cpa_ok=False, last_error="")
        return {"id": account_id, "status": "sso_only", "has_sso": True, "cpa_ok": False}
    except Exception as exc:
        status = "cancelled" if STOP_EVENT.is_set() else "failed"
        error = "login job stopped" if STOP_EVENT.is_set() else _safe_error(exc, email, password, sso)
        update_account(account_id, status=status, sso=sso, cpa_ok=False, last_error=error)
        _log(f"[account-login W{worker_index}] {status}: {error}")
        if status == "failed":
            _log(_safe_traceback(email, password, sso))
        return {"id": account_id, "status": status, "has_sso": bool(sso), "error": error}
    finally:
        if browser_started:
            stop_browser(force=True)
        try:
            runtime.clear_thread_proxy()
        except Exception:
            pass


def _read_job(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("account login job must be an object")
    ids = [str(value or "").strip() for value in data.get("ids") or [] if str(value or "").strip()]
    if not ids:
        raise ValueError("account login job has no account ids")
    return {
        "ids": ids,
        "concurrency": max(1, min(5, int(data.get("concurrency") or 1))),
        "extract_cpa": bool(data.get("extract_cpa")),
        "report_file": str(data.get("report_file") or "").strip(),
        "log_file": str(data.get("log_file") or "").strip(),
        "started_at": str(data.get("created_at") or ""),
    }


def _startup_failure(job: dict, error: object, *, input_count: int = 0) -> int:
    """Persist fatal worker errors before the supervisor resets stale queue rows."""
    message = _safe_error(error)
    detail = f"worker startup failed: {message}"
    for account_id in job.get("ids") or []:
        try:
            update_account(account_id, status="failed", last_error=detail)
        except Exception as update_exc:
            _log(f"[account-login] could not persist startup failure for {account_id}: {update_exc}")
    _log(f"[account-login] startup failed: {message}")
    _log(_safe_traceback())
    report = {
        "ok": False,
        "started_at": job.get("started_at") or "",
        "finished_at": _utc_now(),
        "input_count": input_count or len(job.get("ids") or []),
        "extract_cpa": bool(job.get("extract_cpa")),
        "success_count": 0,
        "sso_only_count": 0,
        "failure_count": len(job.get("ids") or []),
        "cancelled_count": 0,
        "fatal_error": message,
        "log": Path(str(job.get("log_file") or "")).name,
    }
    report_path = Path(job["report_file"]) if job.get("report_file") else ROOT / "log" / "account_login_report.json"
    try:
        atomic_write_json(report_path, report)
    except Exception as exc:
        _log(f"[account-login] could not write failure report: {exc}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Login imported xAI accounts and extract SSO")
    parser.add_argument("--job", required=True, help="private JSON job file")
    args = parser.parse_args(argv)
    _install_signal_handlers()

    job_path = Path(args.job).resolve()
    job = _read_job(job_path)
    try:
        records_by_id = {item["id"]: item for item in private_accounts(job["ids"])}
        records = [records_by_id[item_id] for item_id in job["ids"] if item_id in records_by_id]
        if not records:
            raise RuntimeError("no imported accounts matched the login job")

        import grok_register_ttk as runtime

        runtime.load_config()
        runtime._wire_runtime_modules()
        runtime.load_proxy_pool()
        runtime.config["cpa_auto_add"] = bool(job["extract_cpa"])
    except Exception as exc:
        return _startup_failure(job, exc)

    _log(
        f"[account-login] start accounts={len(records)} workers={job['concurrency']} "
        f"extract_cpa={job['extract_cpa']}"
    )
    results = []
    with ThreadPoolExecutor(max_workers=job["concurrency"], thread_name_prefix="account-login") as pool:
        futures = {
            pool.submit(
                process_one_account,
                record,
                runtime,
                worker_index=index,
                extract_cpa=job["extract_cpa"],
            ): record["id"]
            for index, record in enumerate(records, 1)
        }
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                account_id = futures[future]
                error = _safe_error(exc)
                update_account(account_id, status="failed", last_error=error)
                _log(f"[account-login] account {account_id} failed: {error}")
                _log(_safe_traceback())
                results.append({"id": account_id, "status": "failed", "error": error})

    report = {
        "ok": True,
        "started_at": job["started_at"],
        "finished_at": _utc_now(),
        "input_count": len(records),
        "extract_cpa": job["extract_cpa"],
        "success_count": sum(1 for item in results if item.get("status") == "success"),
        "sso_only_count": sum(1 for item in results if item.get("status") == "sso_only"),
        "failure_count": sum(1 for item in results if item.get("status") == "failed"),
        "cancelled_count": sum(1 for item in results if item.get("status") == "cancelled"),
        "log": Path(str(job.get("log_file") or "")).name,
    }
    report_path = Path(job["report_file"]) if job["report_file"] else ROOT / "log" / "account_login_report.json"
    atomic_write_json(report_path, report)
    _log(
        f"[account-login] finished success={report['success_count']} "
        f"sso_only={report['sso_only_count']} failed={report['failure_count']} "
        f"cancelled={report['cancelled_count']}"
    )
    return 0 if report["failure_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

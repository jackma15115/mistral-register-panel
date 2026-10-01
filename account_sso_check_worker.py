#!/usr/bin/env python3
"""Check the current merged account inventory by exchanging each stored SSO."""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from secure_files import atomic_write_json
from sso_to_auth_json import sso_to_token
from sso_utils import normalize_sso_token
from webui.account_login_store import private_account_inventory
from webui.security_utils import redact_log_line


STOP_EVENT = threading.Event()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fingerprint(sso: object) -> str:
    value = normalize_sso_token(sso)
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else "missing"


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


def _write_report(path: Path, report: dict) -> None:
    report["checked_count"] = len(report["items"])
    report["valid_count"] = sum(1 for item in report["items"] if item["status"] == "valid")
    report["invalid_count"] = sum(1 for item in report["items"] if item["status"] == "invalid")
    atomic_write_json(path, report)


def _read_job(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    ids = [str(value or "").strip() for value in data.get("ids") or [] if str(value or "").strip()]
    if not ids:
        raise ValueError("SSO check job has no account ids")
    return {
        "ids": ids,
        "concurrency": max(1, min(5, int(data.get("concurrency") or 1))),
        "report_file": Path(str(data.get("report_file") or "")).resolve(),
        "created_at": str(data.get("created_at") or ""),
    }


def _check_one_record(
    record: dict,
    runtime,
    *,
    worker_index: int,
    total: int,
    prefer: str,
) -> dict | None:
    if STOP_EVENT.is_set():
        return None
    email = str(record.get("email") or "").strip().lower()
    sso = normalize_sso_token(record.get("sso"))
    row = {
        "id": record["id"],
        "email": email,
        "status": "invalid",
        "reason": "sso_missing" if not sso else "token_exchange_failed",
        "error": "",
        "sso_fingerprint": _fingerprint(sso),
        "checked_at": _utc_now(),
    }
    if not sso:
        _log(f"[account-sso-check {worker_index}/{total}] missing SSO: {email}")
        return row

    proxy = ""
    try:
        proxy = runtime.pick_proxy_for_worker(worker_index, 0)
        token = sso_to_token(
            sso,
            proxy=proxy,
            log=lambda message: _log(f"[account-sso-check {worker_index}] {message}"),
            prefer=prefer,
            allow_fallback=True,
        )
        if token and token.get("access_token"):
            row.update({"status": "valid", "reason": "token_exchange_succeeded"})
    except Exception as exc:
        row["reason"] = "token_exchange_error"
        row["error"] = _safe_error(exc, sso, email, proxy)
    finally:
        try:
            runtime.clear_thread_proxy()
        except Exception:
            pass
        try:
            runtime.release_proxy_lease(worker_index)
        except Exception:
            pass
    return row


def _check_records(records: list[dict], runtime, *, job: dict, prefer: str, report: dict) -> None:
    completed: dict[int, dict] = {}
    with ThreadPoolExecutor(
        max_workers=job["concurrency"],
        thread_name_prefix="account-sso-check",
    ) as pool:
        futures = {
            pool.submit(
                _check_one_record,
                record,
                runtime,
                worker_index=index,
                total=len(records),
                prefer=prefer,
            ): index
            for index, record in enumerate(records, 1)
        }
        for future in as_completed(futures):
            index = futures[future]
            row = future.result()
            if row is None:
                report["cancelled"] = True
            else:
                completed[index] = row
                report["items"] = [completed[key] for key in sorted(completed)]
            _write_report(job["report_file"], report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check all stored account SSO values")
    parser.add_argument("--job", required=True, help="owner-only ID-only job file")
    args = parser.parse_args(argv)
    _install_signal_handlers()

    job = _read_job(Path(args.job).resolve())
    report = {
        "ok": True,
        "job_kind": "sso_check",
        "running": True,
        "started_at": job["created_at"] or _utc_now(),
        "finished_at": "",
        "input_count": len(job["ids"]),
        "concurrency": job["concurrency"],
        "checked_count": 0,
        "valid_count": 0,
        "invalid_count": 0,
        "cancelled": False,
        "items": [],
    }
    try:
        records_by_id = {item["id"]: item for item in private_account_inventory()}
        records = [records_by_id[item_id] for item_id in job["ids"] if item_id in records_by_id]
        if len(records) != len(job["ids"]):
            raise RuntimeError("one or more accounts changed before the SSO check started")

        import grok_register_ttk as runtime

        runtime.load_config()
        runtime._wire_runtime_modules()
        runtime.load_proxy_pool()
        mode = str(runtime.config.get("cpa_token_mode") or "device_protocol").lower()
        prefer = "auth_code" if mode == "auth_code" else "device"
        _write_report(job["report_file"], report)
    except Exception as exc:
        report.update(
            {
                "ok": False,
                "running": False,
                "finished_at": _utc_now(),
                "fatal_error": _safe_error(exc),
            }
        )
        _write_report(job["report_file"], report)
        _log(f"[account-sso-check] startup failed: {report['fatal_error']}")
        return 1

    _log(
        f"[account-sso-check] start accounts={len(records)} "
        f"workers={job['concurrency']}"
    )
    worker_failed = False
    try:
        _check_records(records, runtime, job=job, prefer=prefer, report=report)
    except Exception as exc:
        worker_failed = True
        report["ok"] = False
        report["fatal_error"] = _safe_error(exc)
        _log(f"[account-sso-check] worker failed: {report['fatal_error']}")

    report["running"] = False
    report["finished_at"] = _utc_now()
    _write_report(job["report_file"], report)
    _log(
        f"[account-sso-check] finished valid={report['valid_count']} "
        f"invalid={report['invalid_count']} cancelled={report['cancelled']}"
    )
    return 1 if worker_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Operations for checking and cleaning merged account SSO inventory."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    from secure_files import atomic_write_json, best_effort_fchmod, ensure_private_dir
    from runtime_platform import runtime_python
    from webui.account_login_store import (
        CONFIG_FILE,
        delete_account_resources,
        private_account_inventory,
    )
    from webui.process_utils import find_managed_processes, terminate_managed_processes, write_pid_file
    from webui.security_utils import redact_log_line
except ImportError:  # running from webui/
    ROOT = Path(__file__).resolve().parent.parent
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from secure_files import atomic_write_json, best_effort_fchmod, ensure_private_dir
    from runtime_platform import runtime_python  # type: ignore
    from account_login_store import CONFIG_FILE, delete_account_resources, private_account_inventory  # type: ignore
    from process_utils import find_managed_processes, terminate_managed_processes, write_pid_file  # type: ignore
    from security_utils import redact_log_line  # type: ignore

from sso_utils import normalize_sso_token


ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "log"
WORKER_SCRIPT = ROOT / "account_sso_check_worker.py"
JOB_FILE = Path(os.environ.get("ACCOUNT_SSO_CHECK_JOB_FILE", str(LOG_DIR / "account_sso_check_job.json")))
REPORT_FILE = Path(os.environ.get("ACCOUNT_SSO_CHECK_REPORT_FILE", str(LOG_DIR / "account_sso_check_report.json")))
PID_FILE = Path(os.environ.get("ACCOUNT_SSO_CHECK_PID_FILE", str(LOG_DIR / "account_sso_check.pid")))
VENV_PY = runtime_python(ROOT)


def _workers() -> list[dict]:
    return find_managed_processes(ROOT, ("account_sso_check_worker.py",))


def _all_account_workers() -> list[dict]:
    return find_managed_processes(
        ROOT,
        ("account_login_worker.py", "account_sso_match_worker.py", "account_sso_check_worker.py"),
    )


def _read_private_report() -> dict:
    try:
        data = json.loads(REPORT_FILE.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _public_report(report: dict) -> dict:
    items = []
    for raw in report.get("items") or []:
        if not isinstance(raw, dict):
            continue
        items.append(
            {
                "id": str(raw.get("id") or "")[:40],
                "email": str(raw.get("email") or "")[:254],
                "status": "valid" if raw.get("status") == "valid" else "invalid",
                "reason": str(raw.get("reason") or "")[:80],
                "error": redact_log_line(str(raw.get("error") or ""))[:240],
                "checked_at": str(raw.get("checked_at") or "")[:40],
            }
        )
    return {
        "started_at": str(report.get("started_at") or "")[:40],
        "finished_at": str(report.get("finished_at") or "")[:40],
        "input_count": int(report.get("input_count") or 0),
        "concurrency": max(1, min(5, int(report.get("concurrency") or 1))),
        "checked_count": int(report.get("checked_count") or 0),
        "valid_count": int(report.get("valid_count") or 0),
        "invalid_count": int(report.get("invalid_count") or 0),
        "cancelled": bool(report.get("cancelled")),
        "fatal_error": redact_log_line(str(report.get("fatal_error") or ""))[:240],
        "items": items,
    }


def sso_check_status(*, workers: list[dict] | None = None, report: dict | None = None) -> dict:
    workers = _workers() if workers is None else workers
    report = _public_report(_read_private_report() if report is None else report)
    return {
        **report,
        "running": bool(workers),
        "pid": workers[0]["pid"] if workers else None,
    }


def sso_check_annotations(
    *, private_items: list[dict] | None = None, report: dict | None = None
) -> dict[str, dict]:
    report = _read_private_report() if report is None else report
    current = {
        item["id"]: item
        for item in (private_items if private_items is not None else private_account_inventory())
    }
    annotations: dict[str, dict] = {}
    for raw in report.get("items") or []:
        if not isinstance(raw, dict):
            continue
        account_id = str(raw.get("id") or "")
        account = current.get(account_id)
        if account is None or _fingerprint(account.get("sso")) != str(raw.get("sso_fingerprint") or ""):
            continue
        public = _public_report({"items": [raw]})["items"]
        if public:
            annotations[account_id] = public[0]
    return annotations


def start_sso_check(*, concurrency: object = 1) -> dict:
    if find_managed_processes(ROOT, ("run_until_100.py", "run_batch_headless.py")):
        return {"ok": False, "error": "registration task is running"}
    if find_managed_processes(ROOT, ("sso_to_auth_json.py",)):
        return {"ok": False, "error": "account recovery is running"}
    existing = _all_account_workers()
    if existing:
        return {"ok": False, "error": "an account task is already running", "pid": existing[0]["pid"]}
    if not WORKER_SCRIPT.is_file():
        return {"ok": False, "error": f"missing worker script: {WORKER_SCRIPT}"}
    records = private_account_inventory()
    if not records:
        return {"ok": False, "error": "no accounts are available for SSO checking"}
    try:
        workers = max(1, min(5, int(concurrency or 1)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "concurrency must be an integer from 1 to 5"}

    ensure_private_dir(LOG_DIR)
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    job = {
        "version": 1,
        "ids": [item["id"] for item in records],
        "concurrency": workers,
        "report_file": str(REPORT_FILE),
        "created_at": created_at,
    }
    atomic_write_json(JOB_FILE, job)
    atomic_write_json(
        REPORT_FILE,
        {
            "ok": True,
            "job_kind": "sso_check",
            "running": True,
            "started_at": created_at,
            "finished_at": "",
            "input_count": len(records),
            "concurrency": workers,
            "checked_count": 0,
            "valid_count": 0,
            "invalid_count": 0,
            "cancelled": False,
            "items": [],
        },
    )
    log_path = LOG_DIR / f"account-sso-check-{time.strftime('%Y%m%d-%H%M%S')}.log"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    best_effort_fchmod(fd, 0o600)
    output = os.fdopen(fd, "w", encoding="utf-8")
    command = [str(VENV_PY if VENV_PY.is_file() else Path(sys.executable)), "-u", str(WORKER_SCRIPT), "--job", str(JOB_FILE)]
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except Exception as exc:
        output.write(f"[account-sso-check] worker launch failed: {redact_log_line(str(exc))[:240]}\n")
        output.flush()
        output.close()
        atomic_write_json(
            REPORT_FILE,
            {
                "ok": False,
                "job_kind": "sso_check",
                "running": False,
                "started_at": created_at,
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "input_count": len(records),
                "concurrency": workers,
                "checked_count": 0,
                "valid_count": 0,
                "invalid_count": 0,
                "cancelled": False,
                "items": [],
                "fatal_error": redact_log_line(str(exc))[:240],
            },
        )
        return {"ok": False, "error": "could not start SSO check worker"}
    finally:
        if not output.closed:
            output.close()
    write_pid_file(PID_FILE, process.pid)
    return {
        "ok": True,
        "running": True,
        "job_kind": "sso_check",
        "pid": process.pid,
        "input_count": len(records),
        "concurrency": workers,
    }


def stop_sso_check() -> dict:
    killed = terminate_managed_processes(ROOT, ("account_sso_check_worker.py",))
    return {"ok": True, "killed": killed, "status": sso_check_status()}


def _fingerprint(sso: object) -> str:
    value = normalize_sso_token(sso)
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else "missing"


def _remote_cpa_configured() -> bool:
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return False
    return bool(str(config.get("cpa_remote_url") or "").strip()) if isinstance(config, dict) else False


def delete_checked_invalid_accounts(ids: object) -> dict:
    if find_managed_processes(
        ROOT,
        (
            "run_until_100.py",
            "run_batch_headless.py",
            "sso_to_auth_json.py",
            "account_login_worker.py",
            "account_sso_match_worker.py",
            "account_sso_check_worker.py",
        ),
    ):
        return {"ok": False, "error": "stop running account tasks before deleting data"}
    requested = {str(value or "").strip() for value in (ids or []) if str(value or "").strip()}
    if not requested:
        raise ValueError("no invalid account ids selected")
    current = {item["id"]: item for item in private_account_inventory()}
    if not requested.issubset(current):
        raise ValueError("one or more selected accounts no longer exist")
    missing_sso_ids = {
        account_id for account_id in requested if not str(current[account_id].get("sso") or "").strip()
    }
    checked_ids = requested - missing_sso_ids
    report = _read_private_report()
    if checked_ids and (not report.get("finished_at") or report.get("cancelled")):
        raise ValueError("the latest SSO check did not complete")
    invalid = {
        str(item.get("id") or ""): item
        for item in report.get("items") or []
        if isinstance(item, dict) and item.get("status") == "invalid"
    }
    if not checked_ids.issubset(invalid):
        raise ValueError("selected accounts with SSO must be invalid in the latest completed check")
    for account_id in checked_ids:
        account = current[account_id]
        if _fingerprint(account.get("sso")) != str(invalid[account_id].get("sso_fingerprint") or ""):
            raise ValueError("one or more selected accounts changed after the SSO check")
    result = delete_account_resources(
        sorted(requested),
        expected_sso_fingerprints={
            account_id: str(invalid[account_id].get("sso_fingerprint") or "")
            for account_id in checked_ids
        },
    )
    result["remote_cpa_not_deleted"] = _remote_cpa_configured()
    if result["remote_cpa_not_deleted"]:
        result["warning"] = "remote CPA records were not deleted; remove them from the remote service manually"
    return result

"""Asynchronous recovery jobs for pending SSO and account text files."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import copy
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_platform import popen_group_kwargs, runtime_python
from sso_to_auth_json import load_sso_records
from sso_utils import normalize_sso_token

try:
    from secure_files import best_effort_fchmod, ensure_private_dir, exclusive_file_lock
    from webui.process_utils import (
        find_managed_processes,
        terminate_managed_processes,
        write_pid_file,
    )
except ImportError:  # running from webui/
    from secure_files import best_effort_fchmod, ensure_private_dir, exclusive_file_lock
    from process_utils import (  # type: ignore
        find_managed_processes,
        terminate_managed_processes,
        write_pid_file,
    )

ACCOUNTS_DIR = ROOT / "accounts"
PENDING_FILE = ACCOUNTS_DIR / "sso_pending.txt"
RISK_FILE = ACCOUNTS_DIR / "sso_risk_rejected.txt"
CPA_DIR = Path(os.environ.get("CPA_AUTH_DIR", str(ROOT / "cpa_auth")))
LOG_DIR = ROOT / "log"
REPORT_FILE = LOG_DIR / "recovery_report.json"
PID_FILE = LOG_DIR / "recovery.pid"
RECOVERY_SCRIPT = ROOT / "sso_to_auth_json.py"
VENV_PY = runtime_python(ROOT)
CONFIG_FILE = Path(
    os.environ.get("GROK_REGISTER_CONFIG_FILE", str(ROOT / "config.json"))
)

_STATUS_CACHE_TTL = 10.0
_STATUS_CACHE_LOCK = threading.Lock()
_STATUS_BUILD_LOCK = threading.Lock()
_STATUS_CACHE: tuple[tuple, tuple, float, dict] | None = None
_REPORT_CACHE: tuple[tuple, dict] | None = None


def _file_signature(path: Path) -> tuple:
    try:
        stat = path.stat()
    except OSError:
        return (str(path), False)
    return (str(path), True, int(stat.st_mtime_ns), int(stat.st_size))


def _directory_signature(path: Path, suffix: str | None = None) -> tuple:
    try:
        directory = path.stat()
    except OSError:
        return (str(path), False)
    entries = []
    try:
        for item in path.glob(f"*{suffix}" if suffix else "*"):
            if item.is_file():
                entries.append(_file_signature(item))
    except OSError:
        pass
    return (str(path), True, int(directory.st_mtime_ns), tuple(sorted(entries)))


def _status_input_signature() -> tuple:
    return (
        _file_signature(PENDING_FILE),
        _file_signature(RISK_FILE),
        _directory_signature(ACCOUNTS_DIR, ".txt"),
        _directory_signature(CPA_DIR, ".json"),
    )


def _status_path_key() -> tuple:
    return tuple(
        str(path)
        for path in (PENDING_FILE, RISK_FILE, ACCOUNTS_DIR, CPA_DIR, REPORT_FILE)
    )


def _cached_report() -> dict:
    global _REPORT_CACHE
    signature = _file_signature(REPORT_FILE)
    with _STATUS_CACHE_LOCK:
        if _REPORT_CACHE and _REPORT_CACHE[0] == signature:
            return copy.deepcopy(_REPORT_CACHE[1])
    report = _read_report_uncached()
    with _STATUS_CACHE_LOCK:
        _REPORT_CACHE = (signature, copy.deepcopy(report))
    return report


def _parse_line(line: str) -> tuple[str, str] | None:
    raw = str(line or "").strip()
    if not raw or raw.startswith("#"):
        return None
    email = ""
    sso = raw
    if "----" in raw:
        parts = [part.strip() for part in raw.split("----")]
        email = parts[0] if len(parts) >= 2 else ""
        sso = parts[-1]
    sso = normalize_sso_token(sso)
    if len(sso) < 24 or any(ch.isspace() for ch in sso):
        return None
    return email.lower(), sso


def _records_from_file(path: Path) -> dict[str, str]:
    try:
        records = load_sso_records(path=str(path))
    except (OSError, ValueError):
        return {}
    # Keep a stable key for the account identity.  Email is preferred because
    # repeated logins can produce different SSO snapshots for one account.
    # SSO-only records retain token identity and do not merge into named users.
    result: dict[str, str] = {}
    for record in records:
        email = str(record.email or "").strip().lower()
        identity = f"email:{email}" if email else f"sso:{record.sso}"
        result.setdefault(identity, email)
    return result


def _nonempty_line_count(path: Path) -> int:
    try:
        return sum(
            1
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    except OSError:
        return 0


def _account_records() -> dict[str, str]:
    records: dict[str, str] = {}
    if not ACCOUNTS_DIR.is_dir():
        return records
    for path in sorted(ACCOUNTS_DIR.glob("*.txt")):
        if path.name in {"mail_credentials.txt", "sso_risk_rejected.txt", "sso_bfs_flagged.txt"}:
            continue
        for identity, email in _records_from_file(path).items():
            if identity not in records or email:
                records[identity] = email
    return records


def _cpa_emails() -> set[str]:
    emails: set[str] = set()
    if not CPA_DIR.is_dir():
        return emails
    for path in CPA_DIR.glob("xai-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        email = str(data.get("email") or "").strip().lower()
        if email:
            emails.add(email)
    return emails


def _read_report_uncached() -> dict:
    try:
        report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(report, dict):
        return {}
    failures = []
    for item in report.get("failures") or []:
        if not isinstance(item, dict):
            continue
        failures.append(
            {
                "index": int(item.get("index") or 0),
                "email": str(item.get("email") or "")[:120],
                "reason": str(item.get("reason") or "")[:240],
            }
        )
    return {
        "finished_at": report.get("finished_at"),
        "input_count": int(report.get("input_count") or 0),
        "skipped_existing_count": int(report.get("skipped_existing_count") or 0),
        "success_count": int(report.get("success_count") or 0),
        "failure_count": int(report.get("failure_count") or 0),
        "remaining_count": report.get("remaining_count"),
        "failures": failures[:20],
    }


def _read_report() -> dict:
    return _cached_report()


def recovery_status() -> dict:
    global _STATUS_CACHE
    jobs = find_managed_processes(ROOT, ("sso_to_auth_json.py",))
    now = time.monotonic()
    path_key = _status_path_key()
    with _STATUS_CACHE_LOCK:
        cached = _STATUS_CACHE
        if cached and cached[0] == path_key and now - cached[2] < _STATUS_CACHE_TTL:
            result = copy.deepcopy(cached[3])
            result.update({"running": bool(jobs), "pid": jobs[0]["pid"] if jobs else None})
            return result

    with _STATUS_BUILD_LOCK:
        now = time.monotonic()
        signature = _status_input_signature()
        with _STATUS_CACHE_LOCK:
            cached = _STATUS_CACHE
            if (
                cached
                and cached[0] == path_key
                and cached[1] == signature
            ):
                result = copy.deepcopy(cached[3])
                result.update({"running": bool(jobs), "pid": jobs[0]["pid"] if jobs else None})
                _STATUS_CACHE = (
                    path_key,
                    signature,
                    time.monotonic(),
                    copy.deepcopy(result),
                )
                return result
        pending = _records_from_file(PENDING_FILE)
        all_records = _account_records()
        cpa_emails = _cpa_emails()
        recoverable = sum(
            1
            for email in all_records.values()
            if not email or email not in cpa_emails
        )
        result = {
            "ok": True,
            "running": bool(jobs),
            "pid": jobs[0]["pid"] if jobs else None,
            "pending_count": len(pending),
            "account_record_count": len(all_records),
            "recoverable_count": recoverable,
            "risk_rejected_count": _nonempty_line_count(RISK_FILE),
            "last_report": _read_report(),
        }
        with _STATUS_CACHE_LOCK:
            _STATUS_CACHE = (
                path_key,
                signature,
                time.monotonic(),
                copy.deepcopy(result),
            )
        return result


def start_recovery(scope: str = "pending") -> dict:
    normalized_scope = str(scope or "pending").strip().lower()
    if normalized_scope not in ("pending", "accounts"):
        return {"ok": False, "error": f"unknown recovery scope: {normalized_scope}"}
    if find_managed_processes(ROOT, ("run_until_100.py", "run_batch_headless.py")):
        return {"ok": False, "error": "registration task is running"}
    if find_managed_processes(ROOT, ("account_login_worker.py",)):
        return {"ok": False, "error": "account login task is running"}
    existing = find_managed_processes(ROOT, ("sso_to_auth_json.py",))
    if existing:
        return {"ok": False, "error": "recovery already running", "pid": existing[0]["pid"]}
    if not VENV_PY.is_file():
        return {"ok": False, "error": f"missing runtime python: {VENV_PY}"}
    if not CONFIG_FILE.is_file():
        return {"ok": False, "error": f"missing config: {CONFIG_FILE}"}

    status = recovery_status()
    count = status["pending_count"] if normalized_scope == "pending" else status["recoverable_count"]
    if count <= 0:
        return {"ok": False, "error": "no recoverable records", "status": status}

    ensure_private_dir(LOG_DIR)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"recovery-{timestamp}-{normalized_scope}.log"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    best_effort_fchmod(fd, 0o600)
    output = os.fdopen(fd, "w", encoding="utf-8")
    command = [
        str(VENV_PY),
        "-u",
        str(RECOVERY_SCRIPT),
        "--from-config",
        str(CONFIG_FILE),
        "--report-json",
        str(REPORT_FILE),
    ]
    if normalized_scope == "pending":
        command.extend(("--sso", str(PENDING_FILE), "--consume-success"))
    else:
        command.extend(("--accounts-dir", str(ACCOUNTS_DIR)))
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=output,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            **popen_group_kwargs(),
        )
    finally:
        output.close()
    write_pid_file(PID_FILE, process.pid)
    return {
        "ok": True,
        "running": True,
        "pid": process.pid,
        "scope": normalized_scope,
        "input_count": count,
        "log": log_path.name,
    }


def stop_recovery() -> dict:
    killed = terminate_managed_processes(ROOT, ("sso_to_auth_json.py",))
    return {"ok": True, "killed": killed, "status": recovery_status()}

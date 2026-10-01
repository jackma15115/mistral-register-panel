"""Web operation layer for imported-account browser login jobs."""

from __future__ import annotations

import json
import os
import secrets
import signal
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

try:
    from secure_files import atomic_write_json, atomic_write_text, ensure_private_dir
    from webui.account_login_store import (
        delete_accounts,
        import_account_credentials,
        mark_accounts_queued,
        parse_sso_values,
        private_account_inventory,
        private_accounts,
        read_account_inventory,
        reset_incomplete_accounts,
    )
    from webui.account_sso_check_ops import (
        _read_private_report as _read_sso_check_report,
        sso_check_annotations,
        sso_check_status,
    )
    from webui.process_utils import find_managed_processes, terminate_managed_processes, write_pid_file
    from webui.security_utils import redact_log_line
except ImportError:  # running from webui/
    ROOT = Path(__file__).resolve().parent.parent
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from secure_files import atomic_write_json, atomic_write_text, ensure_private_dir
    from account_login_store import (  # type: ignore
        delete_accounts,
        import_account_credentials,
        mark_accounts_queued,
        parse_sso_values,
        private_account_inventory,
        private_accounts,
        read_account_inventory,
        reset_incomplete_accounts,
    )
    from account_sso_check_ops import (  # type: ignore
        _read_private_report as _read_sso_check_report,
        sso_check_annotations,
        sso_check_status,
    )
    from process_utils import find_managed_processes, terminate_managed_processes, write_pid_file  # type: ignore
    from security_utils import redact_log_line  # type: ignore


ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "log"
WORKER_SCRIPT = ROOT / "account_login_worker.py"
SSO_MATCH_WORKER_SCRIPT = ROOT / "account_sso_match_worker.py"
JOB_FILE = Path(os.environ.get("ACCOUNT_LOGIN_JOB_FILE", str(LOG_DIR / "account_login_job.json")))
REPORT_FILE = Path(os.environ.get("ACCOUNT_LOGIN_REPORT_FILE", str(LOG_DIR / "account_login_report.json")))
PID_FILE = Path(os.environ.get("ACCOUNT_LOGIN_PID_FILE", str(LOG_DIR / "account_login.pid")))
VENV_PY = ROOT / ".venv" / "bin" / "python"
_WATCH_LOCK = threading.Lock()
_INTENTIONAL_STOPS: set[int] = set()
_ACTIVE_WATCHERS: set[int] = set()
_ACTIVE_JOB_KINDS: dict[int, str] = {}
_LOG_TAIL_BYTES = 48 * 1024
_LOG_TAIL_LINES = 160


def _workers() -> list[dict]:
    return find_managed_processes(
        ROOT,
        ("account_login_worker.py", "account_sso_match_worker.py", "account_sso_check_worker.py"),
    )


def _latest_log_path() -> Path | None:
    try:
        paths = sorted(
            (
                path
                for path in LOG_DIR.glob("account-*.log")
                if path.is_file() and not path.is_symlink()
            ),
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        return None
    return paths[-1] if paths else None


def _latest_log_name() -> str:
    path = _latest_log_path()
    return path.name[:160] if path else ""


def _read_latest_log_tail() -> dict:
    path = _latest_log_path()
    if path is None:
        return {"name": "", "lines": [], "truncated": False}
    try:
        if path.resolve().parent != LOG_DIR.resolve():
            return {"name": "", "lines": [], "truncated": False}
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            byte_truncated = size > _LOG_TAIL_BYTES
            handle.seek(max(0, size - _LOG_TAIL_BYTES))
            text = handle.read(_LOG_TAIL_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return {"name": path.name[:160], "lines": [], "truncated": False}

    raw_lines = text.splitlines()
    if byte_truncated and raw_lines:
        raw_lines = raw_lines[1:]
    line_truncated = len(raw_lines) > _LOG_TAIL_LINES
    raw_lines = raw_lines[-_LOG_TAIL_LINES:]
    lines = [redact_log_line(line)[:1200] for line in raw_lines]
    return {
        "name": path.name[:160],
        "lines": lines,
        "truncated": bool(byte_truncated or line_truncated),
    }


def _read_report() -> dict:
    try:
        data = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    except Exception:
        log_name = _latest_log_name()
        return {"log": log_name} if log_name else {}
    if not isinstance(data, dict):
        return {}
    return {
        "job_kind": str(data.get("job_kind") or "account_login")[:40],
        "started_at": str(data.get("started_at") or "")[:40],
        "finished_at": str(data.get("finished_at") or "")[:40],
        "log": Path(str(data.get("log") or "")).name[:160] or _latest_log_name(),
        "fatal_error": redact_log_line(str(data.get("fatal_error") or ""))[:240],
        "input_count": int(data.get("input_count") or 0),
        "extract_cpa": bool(data.get("extract_cpa")),
        "success_count": int(data.get("success_count") or 0),
        "sso_only_count": int(data.get("sso_only_count") or 0),
        "failure_count": int(data.get("failure_count") or 0),
        "cancelled_count": int(data.get("cancelled_count") or 0),
        "matched_count": int(data.get("matched_count") or 0),
        "unusable_count": int(data.get("unusable_count") or 0),
        "unmatched_count": int(data.get("unmatched_count") or 0),
        "cpa_success_count": int(data.get("cpa_success_count") or 0),
    }


def _last_log_diagnostic(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 32 * 1024))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    diagnostic_terms = (
        "error",
        "failed",
        "exception",
        "errno",
        "killed",
        "not found",
        "no such",
        "denied",
        "cannot",
        "unable",
        "xvfb",
        "失败",
        "异常",
        "找不到",
        "拒绝",
        "无法",
        "终止",
    )
    for raw_line in reversed(text.splitlines()):
        line = redact_log_line(raw_line).strip()
        if not line or line.startswith('File "') or line.startswith("Traceback ("):
            continue
        if line.startswith("[account-login] start "):
            continue
        if any(term in line.lower() for term in diagnostic_terms):
            return line[:200]
    return ""


def _stale_job_reason() -> str:
    log_name = _latest_log_name()
    diagnostic = _last_log_diagnostic(LOG_DIR / log_name) if log_name else ""
    if diagnostic:
        return f"previous login worker exited: {diagnostic}"
    return "previous login job was interrupted"


def _worker_exit_label(return_code: int) -> str:
    signal_number = -return_code if return_code < 0 else return_code - 128
    if signal_number > 0:
        signal_name = {2: "SIGINT", 9: "SIGKILL", 15: "SIGTERM"}.get(signal_number, "")
        try:
            signal_name = signal.Signals(signal_number).name or signal_name
        except ValueError:
            pass
        if signal_name:
            suffix = "; possible container OOM kill" if signal_name == "SIGKILL" else ""
            return f"login worker exited with code {return_code} ({signal_name}{suffix})"
    return f"login worker exited with code {return_code}"


def _watch_worker(
    process,
    ids: list[str],
    job: dict,
    log_path: Path,
    cleanup_path: Path | None = None,
) -> None:
    try:
        try:
            return_code = int(process.wait())
        except Exception:
            return
        with _WATCH_LOCK:
            intentional_stop = process.pid in _INTENTIONAL_STOPS
        if intentional_stop or return_code == 0:
            return

        diagnostic = _last_log_diagnostic(log_path)
        reason = _worker_exit_label(return_code)
        if diagnostic:
            reason = f"{reason}: {diagnostic}"
        else:
            reason = f"{reason} before writing diagnostics"
        changed = reset_incomplete_accounts(reason, ids)
        if not changed:
            return
        try:
            with log_path.open("a", encoding="utf-8") as output:
                output.write(f"[account-login] supervisor: {reason}\n")
        except OSError:
            pass
        try:
            atomic_write_json(
                REPORT_FILE,
                {
                    "ok": False,
                    "started_at": job["created_at"],
                    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "input_count": len(ids),
                    "extract_cpa": bool(job["extract_cpa"]),
                    "success_count": 0,
                    "sso_only_count": 0,
                    "failure_count": changed,
                    "cancelled_count": 0,
                    "fatal_error": reason,
                    "log": log_path.name,
                },
            )
        except Exception:
            pass
    finally:
        if cleanup_path is not None:
            cleanup_path.unlink(missing_ok=True)
        with _WATCH_LOCK:
            _ACTIVE_WATCHERS.discard(process.pid)
            _ACTIVE_JOB_KINDS.pop(process.pid, None)
            _INTENTIONAL_STOPS.discard(process.pid)


def account_login_status() -> dict:
    workers = _workers()
    with _WATCH_LOCK:
        watcher_pids = sorted(_ACTIVE_WATCHERS)
    if not workers and not watcher_pids:
        # This state-file update is cheap; avoid scanning the latest log on
        # every five-second panel refresh.
        reset_incomplete_accounts()
    private_items = private_account_inventory()
    inventory = read_account_inventory(private_items)
    sso_report = _read_sso_check_report()
    annotations = sso_check_annotations(private_items=private_items, report=sso_report)
    for item in inventory["items"]:
        check = annotations.get(item["id"])
        item["sso_check_status"] = str((check or {}).get("status") or "not_checked")
        item["sso_check_reason"] = str((check or {}).get("reason") or "")
        item["sso_check_error"] = str((check or {}).get("error") or "")
        item["sso_checked_at"] = str((check or {}).get("checked_at") or "")
    inventory["summary"].update(
        {
            "sso_checked": sum(1 for item in inventory["items"] if item["sso_check_status"] != "not_checked"),
            "sso_valid": sum(1 for item in inventory["items"] if item["sso_check_status"] == "valid"),
            "sso_invalid": sum(1 for item in inventory["items"] if item["sso_check_status"] == "invalid"),
        }
    )
    sso_workers = [
        item
        for item in workers
        if "account_sso_check_worker.py" in str(item.get("cmd") or "")
    ]
    check_status = sso_check_status(workers=sso_workers, report=sso_report)
    log_tail = _read_latest_log_tail()
    report = _read_report()
    if workers:
        if any("account_sso_check_worker.py" in str(item.get("cmd") or "") for item in workers):
            job_kind = "sso_check"
        elif any("account_sso_match_worker.py" in str(item.get("cmd") or "") for item in workers):
            job_kind = "sso_match"
        else:
            job_kind = "account_login"
    elif watcher_pids:
        with _WATCH_LOCK:
            job_kind = _ACTIVE_JOB_KINDS.get(watcher_pids[0], "account_login")
    else:
        job_kind = str(report.get("job_kind") or "")
    return {
        **inventory,
        "running": bool(workers or watcher_pids),
        "pid": workers[0]["pid"] if workers else (watcher_pids[0] if watcher_pids else None),
        "job_kind": job_kind,
        "last_report": report,
        "log_tail": log_tail["lines"],
        "log_tail_name": log_tail["name"],
        "log_tail_truncated": log_tail["truncated"],
        "sso_check": check_status,
    }


def import_accounts(text: object) -> dict:
    if _workers():
        return {"ok": False, "error": "account login task is running"}
    return import_account_credentials(text)


def _python_command() -> str:
    return str(VENV_PY if VENV_PY.is_file() else Path(sys.executable))


def start_account_login(
    ids: object = None,
    *,
    concurrency: object = 1,
    extract_cpa: object = False,
    pending_only: bool = False,
    pending_scope: str | None = None,
) -> dict:
    if find_managed_processes(ROOT, ("run_until_100.py", "run_batch_headless.py")):
        return {"ok": False, "error": "registration task is running"}
    if find_managed_processes(ROOT, ("sso_to_auth_json.py",)):
        return {"ok": False, "error": "account recovery is running"}
    existing = _workers()
    if existing:
        return {"ok": False, "error": "account login task already running", "pid": existing[0]["pid"]}
    if pending_scope not in {None, "sso_missing", "cpa_missing"}:
        return {"ok": False, "error": f"unknown account login scope: {pending_scope}"}
    reset_incomplete_accounts()
    if not WORKER_SCRIPT.is_file():
        return {"ok": False, "error": f"missing worker script: {WORKER_SCRIPT}"}

    try:
        workers = max(1, min(5, int(concurrency or 1)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "concurrency must be an integer from 1 to 5"}
    want_cpa = bool(extract_cpa) or pending_scope == "cpa_missing"
    requested = [str(value or "").strip() for value in (ids or []) if str(value or "").strip()]
    records = private_accounts(
        requested,
        pending_only=bool(pending_only or (not requested and pending_scope is None)),
        include_sso_only=want_cpa,
        pending_scope=pending_scope,
    )
    if requested:
        found_ids = {item["id"] for item in records}
        missing = [item_id for item_id in requested if item_id not in found_ids]
        if missing:
            return {
                "ok": False,
                "error": "selected accounts must be present in the imported credential inventory",
            }
    if not records:
        return {"ok": False, "error": "no imported accounts are ready for login"}

    ids_to_run = [item["id"] for item in records]
    ensure_private_dir(LOG_DIR)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"account-login-{timestamp}.log"
    job = {
        "version": 1,
        "ids": ids_to_run,
        "concurrency": workers,
        "extract_cpa": want_cpa,
        "report_file": str(REPORT_FILE),
        "log_file": str(log_path),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    atomic_write_json(JOB_FILE, job)
    mark_accounts_queued(ids_to_run)

    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    output = os.fdopen(fd, "w", encoding="utf-8")
    command = [_python_command(), "-u", str(WORKER_SCRIPT), "--job", str(JOB_FILE)]
    if os.name != "nt" and shutil.which("xvfb-run"):
        command = ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24", *command]
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        with _WATCH_LOCK:
            _ACTIVE_WATCHERS.add(process.pid)
            _ACTIVE_JOB_KINDS[process.pid] = "account_login"
    except Exception as exc:
        error = redact_log_line(f"{type(exc).__name__}: {exc}")[:240]
        output.write(f"[account-login] worker launch failed: {error}\n")
        output.write(redact_log_line(traceback.format_exc()))
        output.flush()
        reset_incomplete_accounts(f"could not start login worker: {error}")
        try:
            atomic_write_json(
                REPORT_FILE,
                {
                    "ok": False,
                    "started_at": job["created_at"],
                    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "input_count": len(ids_to_run),
                    "extract_cpa": want_cpa,
                    "success_count": 0,
                    "sso_only_count": 0,
                    "failure_count": len(ids_to_run),
                    "cancelled_count": 0,
                    "fatal_error": error,
                    "log": log_path.name,
                },
            )
        except Exception:
            pass
        raise
    finally:
        output.close()
    write_pid_file(PID_FILE, process.pid)
    threading.Thread(
        target=_watch_worker,
        args=(process, ids_to_run, job, log_path),
        name=f"account-login-watch-{process.pid}",
        daemon=True,
    ).start()
    return {
        "ok": True,
        "running": True,
        "pid": process.pid,
        "input_count": len(ids_to_run),
        "concurrency": workers,
        "extract_cpa": want_cpa,
        "log": log_path.name,
    }


def start_account_sso_match(text: object) -> dict:
    values = parse_sso_values(text)
    if find_managed_processes(ROOT, ("run_until_100.py", "run_batch_headless.py")):
        return {"ok": False, "error": "registration task is running"}
    if find_managed_processes(ROOT, ("sso_to_auth_json.py",)):
        return {"ok": False, "error": "account recovery is running"}
    existing = _workers()
    if existing:
        return {"ok": False, "error": "account login task already running", "pid": existing[0]["pid"]}
    if not SSO_MATCH_WORKER_SCRIPT.is_file():
        return {"ok": False, "error": f"missing worker script: {SSO_MATCH_WORKER_SCRIPT}"}
    if not private_accounts():
        return {"ok": False, "error": "import account credentials before matching SSO"}

    ensure_private_dir(LOG_DIR)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    nonce = secrets.token_hex(4)
    stem = f"account-login-sso-match-{timestamp}-{nonce}"
    input_path = LOG_DIR / f".{stem}.input"
    log_path = LOG_DIR / f"{stem}.log"
    atomic_write_text(input_path, "\n".join(values) + "\n")

    try:
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except Exception:
        input_path.unlink(missing_ok=True)
        raise
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    output = os.fdopen(fd, "w", encoding="utf-8")
    command = [
        _python_command(),
        "-u",
        str(SSO_MATCH_WORKER_SCRIPT),
        "--input-file",
        str(input_path),
        "--report-file",
        str(REPORT_FILE),
        "--log-file",
        log_path.name,
    ]
    job = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "extract_cpa": True,
    }
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        with _WATCH_LOCK:
            _ACTIVE_WATCHERS.add(process.pid)
            _ACTIVE_JOB_KINDS[process.pid] = "sso_match"
    except Exception as exc:
        input_path.unlink(missing_ok=True)
        error = redact_log_line(f"{type(exc).__name__}: {exc}")[:240]
        output.write(f"[account-sso-match] worker launch failed: {error}\n")
        output.flush()
        atomic_write_json(
            REPORT_FILE,
            {
                "ok": False,
                "job_kind": "sso_match",
                "started_at": job["created_at"],
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "input_count": len(values),
                "matched_count": 0,
                "unusable_count": 0,
                "unmatched_count": 0,
                "cpa_success_count": 0,
                "sso_only_count": 0,
                "failure_count": len(values),
                "cancelled_count": 0,
                "fatal_error": error,
                "log": log_path.name,
            },
        )
        raise
    finally:
        output.close()

    write_pid_file(PID_FILE, process.pid)
    threading.Thread(
        target=_watch_worker,
        args=(process, [], job, log_path, input_path),
        name=f"account-sso-match-watch-{process.pid}",
        daemon=True,
    ).start()
    return {
        "ok": True,
        "running": True,
        "job_kind": "sso_match",
        "pid": process.pid,
        "input_count": len(values),
        "log": log_path.name,
    }


def stop_account_login() -> dict:
    workers = _workers()
    with _WATCH_LOCK:
        _INTENTIONAL_STOPS.update(int(item["pid"]) for item in workers)
    killed = terminate_managed_processes(
        ROOT,
        ("account_login_worker.py", "account_sso_match_worker.py", "account_sso_check_worker.py"),
    )
    reset_incomplete_accounts()
    return {"ok": True, "killed": killed, "status": account_login_status()}


def delete_imported_accounts(ids: object) -> dict:
    if _workers():
        return {"ok": False, "error": "stop the account login task before deleting accounts"}
    return delete_accounts(ids)

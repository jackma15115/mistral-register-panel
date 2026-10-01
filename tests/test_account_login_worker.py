# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import account_login_worker as worker


def test_worker_writes_canonical_account_file_and_cpa():
    with tempfile.TemporaryDirectory() as temp:
        account_path = Path(temp) / "person@example.test.txt"
        updates = []
        runtime = SimpleNamespace(
            pick_proxy_for_worker=lambda *_args: "",
            set_thread_proxy=lambda _value: None,
            clear_thread_proxy=lambda: None,
            record_proxy_boot_failure=lambda *_args: None,
            account_file_for_email=lambda _email: str(account_path),
            add_sso_to_cpa=lambda _sso, **_kwargs: True,
        )
        fake_flow = SimpleNamespace(login_and_extract_sso=lambda *_args, **_kwargs: "-private-sso-token")
        fake_browser = SimpleNamespace(
            start_browser=lambda **_kwargs: (object(), object()),
            stop_browser=lambda **_kwargs: None,
        )
        record = {
            "id": "a" * 20,
            "email": "person@example.test",
            "password": "private-password",
            "sso": "",
        }
        worker.STOP_EVENT.clear()
        with patch.dict(sys.modules, {"account_login_flow": fake_flow, "browser_session": fake_browser}), patch.object(
            worker, "update_account", side_effect=lambda account_id, **values: updates.append((account_id, values))
        ):
            result = worker.process_one_account(record, runtime, worker_index=1, extract_cpa=True)

        assert result["status"] == "success"
        assert account_path.read_text(encoding="utf-8") == (
            "person@example.test----private-password----private-sso-token\n"
        )
        assert any(values.get("status") == "success" and values.get("cpa_ok") is True for _, values in updates)


def test_worker_persists_startup_failure_details():
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        job_path = base / "account_login_job.json"
        report_path = base / "account_login_report.json"
        job_path.write_text(
            json.dumps(
                {
                    "ids": ["b" * 20],
                    "concurrency": 1,
                    "extract_cpa": False,
                    "report_file": str(report_path),
                    "log_file": str(base / "account-login-test.log"),
                    "created_at": "2026-07-31T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        updates = []
        with patch.object(worker, "private_accounts", side_effect=RuntimeError("runtime setup failed")), patch.object(
            worker, "update_account", side_effect=lambda account_id, **values: updates.append((account_id, values))
        ):
            result = worker.main(["--job", str(job_path)])

        assert result == 1
        assert updates == [("b" * 20, {"status": "failed", "last_error": "worker startup failed: runtime setup failed"})]
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["fatal_error"] == "runtime setup failed"
        assert report["failure_count"] == 1
        assert report["log"] == "account-login-test.log"


def test_worker_failure_logs_traceback_without_account_secrets():
    runtime = SimpleNamespace(
        pick_proxy_for_worker=lambda *_args: "",
        set_thread_proxy=lambda _value: None,
        clear_thread_proxy=lambda: None,
        record_proxy_boot_failure=lambda *_args: None,
    )

    def fail_login(*_args, **_kwargs):
        raise RuntimeError("browser failed for person@example.test using private-password")

    fake_flow = SimpleNamespace(login_and_extract_sso=fail_login)
    fake_browser = SimpleNamespace(
        start_browser=lambda **_kwargs: (object(), object()),
        stop_browser=lambda **_kwargs: None,
    )
    record = {
        "id": "c" * 20,
        "email": "person@example.test",
        "password": "private-password",
        "sso": "",
    }
    output = io.StringIO()
    worker.STOP_EVENT.clear()
    with patch.dict(sys.modules, {"account_login_flow": fake_flow, "browser_session": fake_browser}), patch.object(
        worker, "update_account", return_value=record
    ), redirect_stdout(output):
        result = worker.process_one_account(record, runtime, worker_index=1, extract_cpa=False)

    log_text = output.getvalue()
    assert result["status"] == "failed"
    assert "Traceback (most recent call last)" in log_text
    assert "private-password" not in log_text
    assert "person@example.test" not in log_text


if __name__ == "__main__":
    test_worker_writes_canonical_account_file_and_cpa()
    test_worker_persists_startup_failure_details()
    test_worker_failure_logs_traceback_without_account_secrets()
    print("OK account login worker")

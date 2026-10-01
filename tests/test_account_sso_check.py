# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webui import account_login_store as store
from webui import account_sso_check_ops as ops
import account_sso_check_worker as worker


def test_inventory_merges_registered_accounts_and_redacts_secrets():
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        previous = (
            store.STATE_PATH,
            store.LOCK_PATH,
            store.ACCOUNT_FILES_DIR,
            store.CONFIG_FILE,
        )
        store.STATE_PATH = base / "imported_credentials.json"
        store.LOCK_PATH = base / "imported_credentials.json.lock"
        store.ACCOUNT_FILES_DIR = base / "accounts"
        store.CONFIG_FILE = base / "config.json"
        (base / "accounts").mkdir()
        (base / "accounts" / "registered@example.test.txt").write_text(
            "registered@example.test----registered-pass----" + "r" * 80 + "\n",
            encoding="utf-8",
        )
        (base / "accounts" / "accounts_20260101.txt").write_text(
            "batch@example.test----batch-pass----" + "b" * 80 + "\n",
            encoding="utf-8",
        )
        try:
            store.import_account_credentials("registered@example.test----imported-pass")
            data = store.read_account_inventory()
            assert [item["email"] for item in data["items"]] == ["registered@example.test"]
            item = data["items"][0]
            assert item["source"] == "both"
            assert item["has_password"] is True
            assert item["has_sso"] is True
            encoded = json.dumps(data)
            assert "registered-pass" not in encoded
            assert "r" * 80 not in encoded
        finally:
            (
                store.STATE_PATH,
                store.LOCK_PATH,
                store.ACCOUNT_FILES_DIR,
                store.CONFIG_FILE,
            ) = previous


def test_delete_resources_removes_local_chain_and_side_queue():
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        previous = (
            store.STATE_PATH,
            store.LOCK_PATH,
            store.ACCOUNT_FILES_DIR,
            store.CONFIG_FILE,
        )
        store.STATE_PATH = base / "accounts" / "imported_credentials.json"
        store.LOCK_PATH = base / "accounts" / "imported_credentials.json.lock"
        store.ACCOUNT_FILES_DIR = base / "accounts"
        store.CONFIG_FILE = base / "config.json"
        cpa = base / "cpa"
        g2a = base / "g2a"
        cpa.mkdir()
        g2a.mkdir()
        store.CONFIG_FILE.write_text(
            json.dumps({"cpa_auth_dir": str(cpa), "grok2api_auth_dir": str(g2a)}),
            encoding="utf-8",
        )
        try:
            account = "remove@example.test"
            sso = "s" * 80
            store.import_account_credentials(f"{account}----password----{sso}")
            (store.ACCOUNT_FILES_DIR / "sso_pending.txt").write_text(
                f"{account}----{sso}\nkeep@example.test----{'k' * 80}\n",
                encoding="utf-8",
            )
            (cpa / "xai-remove@example.test.json").write_text(
                json.dumps({"email": account}), encoding="utf-8"
            )
            (g2a / "g2a-remove@example.test.json").write_text(
                json.dumps({"email": account}), encoding="utf-8"
            )
            item = store.private_account_inventory()[0]
            result = store.delete_account_resources([item["id"]])
            assert result["ok"] is True
            assert result["deleted"] == 1
            assert not (store.ACCOUNT_FILES_DIR / "remove@example.test.txt").exists()
            assert not (cpa / "xai-remove@example.test.json").exists()
            assert not (g2a / "g2a-remove@example.test.json").exists()
            assert "remove@example.test" not in (store.ACCOUNT_FILES_DIR / "sso_pending.txt").read_text(encoding="utf-8")
            assert not store.private_accounts()
        finally:
            (
                store.STATE_PATH,
                store.LOCK_PATH,
                store.ACCOUNT_FILES_DIR,
                store.CONFIG_FILE,
            ) = previous


def test_delete_requires_completed_latest_invalid_check():
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        previous_store = store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR
        previous = ops.REPORT_FILE
        store.STATE_PATH = base / "accounts" / "imported_credentials.json"
        store.LOCK_PATH = base / "accounts" / "imported_credentials.json.lock"
        store.ACCOUNT_FILES_DIR = base / "accounts"
        ops.REPORT_FILE = Path(temp) / "report.json"
        ops.REPORT_FILE.write_text(json.dumps({"items": []}), encoding="utf-8")
        try:
            store.import_account_credentials(f"checked@example.test----password----{'s' * 80}")
            item = store.private_account_inventory()[0]
            with patch.object(ops, "find_managed_processes", return_value=[]):
                try:
                    ops.delete_checked_invalid_accounts([item["id"]])
                except ValueError as exc:
                    assert "did not complete" in str(exc)
                else:
                    raise AssertionError("expected incomplete report rejection")
        finally:
            store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR = previous_store
            ops.REPORT_FILE = previous


def test_delete_allows_missing_sso_without_a_completed_check():
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        previous_store = store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR
        previous_report = ops.REPORT_FILE
        store.STATE_PATH = base / "accounts" / "imported_credentials.json"
        store.LOCK_PATH = base / "accounts" / "imported_credentials.json.lock"
        store.ACCOUNT_FILES_DIR = base / "accounts"
        ops.REPORT_FILE = base / "report.json"
        ops.REPORT_FILE.write_text(json.dumps({"items": []}), encoding="utf-8")
        try:
            store.import_account_credentials("missing@example.test----password")
            item = store.private_account_inventory()[0]
            with patch.object(ops, "find_managed_processes", return_value=[]):
                result = ops.delete_checked_invalid_accounts([item["id"]])
            assert result["ok"] is True
            assert result["deleted"] == 1
            assert not store.private_accounts()
        finally:
            store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR = previous_store
            ops.REPORT_FILE = previous_report


def test_delete_resources_rechecks_sso_before_removing_data():
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        previous = store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR
        store.STATE_PATH = base / "accounts" / "imported_credentials.json"
        store.LOCK_PATH = base / "accounts" / "imported_credentials.json.lock"
        store.ACCOUNT_FILES_DIR = base / "accounts"
        try:
            store.import_account_credentials(f"change@example.test----password----{'a' * 80}")
            item = store.private_accounts()[0]
            try:
                store.delete_account_resources(
                    [item["id"]],
                    expected_sso_fingerprints={item["id"]: "not-the-current-sso"},
                )
            except store.AccountImportError as exc:
                assert "changed" in str(exc)
            else:
                raise AssertionError("expected stale SSO rejection")
            assert store.private_accounts()
        finally:
            store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR = previous


def test_start_sso_check_reuses_login_concurrency_limit():
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        worker_script = base / "account_sso_check_worker.py"
        worker_script.write_text("# test worker\n", encoding="utf-8")
        previous = (
            ops.LOG_DIR,
            ops.WORKER_SCRIPT,
            ops.JOB_FILE,
            ops.REPORT_FILE,
            ops.PID_FILE,
            ops.VENV_PY,
        )
        ops.LOG_DIR = base / "log"
        ops.WORKER_SCRIPT = worker_script
        ops.JOB_FILE = ops.LOG_DIR / "account_sso_check_job.json"
        ops.REPORT_FILE = ops.LOG_DIR / "account_sso_check_report.json"
        ops.PID_FILE = ops.LOG_DIR / "account_sso_check.pid"
        ops.VENV_PY = base / ".venv" / "Scripts" / "python.exe"
        record = {
            "id": "c" * 20,
            "email": "check@example.test",
            "sso": "s" * 80,
        }
        try:
            with patch.object(ops, "find_managed_processes", return_value=[]), patch.object(
                ops, "private_account_inventory", return_value=[record]
            ), patch.object(
                ops.subprocess, "Popen", return_value=SimpleNamespace(pid=4321)
            ):
                result = ops.start_sso_check(concurrency=9)
            assert result["ok"] is True
            assert result["concurrency"] == 5
            job = json.loads(ops.JOB_FILE.read_text(encoding="utf-8"))
            report = json.loads(ops.REPORT_FILE.read_text(encoding="utf-8"))
            assert job["concurrency"] == 5
            assert report["concurrency"] == 5
        finally:
            (
                ops.LOG_DIR,
                ops.WORKER_SCRIPT,
                ops.JOB_FILE,
                ops.REPORT_FILE,
                ops.PID_FILE,
                ops.VENV_PY,
            ) = previous


def test_sso_check_worker_runs_configured_concurrency_and_serializes_reports():
    with tempfile.TemporaryDirectory() as temp:
        report_path = Path(temp) / "report.json"
        records = [
            {
                "id": str(index) * 20,
                "email": f"person{index}@example.test",
                "sso": str(index) * 80,
            }
            for index in range(1, 4)
        ]
        report = {
            "items": [],
            "checked_count": 0,
            "valid_count": 0,
            "invalid_count": 0,
            "cancelled": False,
        }
        barrier = threading.Barrier(3)
        activity_lock = threading.Lock()
        activity = {"active": 0, "peak": 0}

        class Runtime:
            def __init__(self):
                self.released = []

            @staticmethod
            def pick_proxy_for_worker(worker_index, _rotate_index):
                return f"http://proxy{worker_index}.example:8080"

            @staticmethod
            def clear_thread_proxy():
                return None

            def release_proxy_lease(self, worker_index):
                self.released.append(worker_index)

        def exchange(_sso, **_kwargs):
            with activity_lock:
                activity["active"] += 1
                activity["peak"] = max(activity["peak"], activity["active"])
            try:
                barrier.wait(timeout=5)
                return {"access_token": "example-access-token"}
            finally:
                with activity_lock:
                    activity["active"] -= 1

        runtime = Runtime()
        worker.STOP_EVENT.clear()
        try:
            with patch.object(worker, "sso_to_token", side_effect=exchange):
                worker._check_records(
                    records,
                    runtime,
                    job={"concurrency": 3, "report_file": report_path},
                    prefer="device",
                    report=report,
                )
        finally:
            worker.STOP_EVENT.clear()

        saved = json.loads(report_path.read_text(encoding="utf-8"))
        assert activity["peak"] == 3
        assert saved["checked_count"] == 3
        assert saved["valid_count"] == 3
        assert [item["id"] for item in saved["items"]] == [item["id"] for item in records]
        assert sorted(runtime.released) == [1, 2, 3]


if __name__ == "__main__":
    test_inventory_merges_registered_accounts_and_redacts_secrets()
    test_delete_resources_removes_local_chain_and_side_queue()
    test_delete_requires_completed_latest_invalid_check()
    test_delete_allows_missing_sso_without_a_completed_check()
    test_delete_resources_rechecks_sso_before_removing_data()
    test_start_sso_check_reuses_login_concurrency_limit()
    test_sso_check_worker_runs_configured_concurrency_and_serializes_reports()
    print("OK account sso check")

# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webui import account_login_store
from webui import bfs_ops
from webui import monitor
from webui import recovery_ops
from webui import sso_state_ops


def test_account_inventory_reuses_unchanged_parsed_snapshot():
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        previous = (
            account_login_store.STATE_PATH,
            account_login_store.LOCK_PATH,
            account_login_store.ACCOUNT_FILES_DIR,
            account_login_store.CONFIG_FILE,
            account_login_store._INVENTORY_CACHE_TTL,
            account_login_store._INVENTORY_CACHE,
        )
        account_login_store.STATE_PATH = base / "accounts" / "imported_credentials.json"
        account_login_store.LOCK_PATH = base / "accounts" / "imported_credentials.json.lock"
        account_login_store.ACCOUNT_FILES_DIR = base / "accounts"
        account_login_store.CONFIG_FILE = base / "config.json"
        account_login_store._INVENTORY_CACHE_TTL = 0
        account_login_store._INVENTORY_CACHE = None
        try:
            with patch.object(
                account_login_store, "_inventory_signature", return_value=("unchanged",)
            ), patch.object(
                account_login_store, "_registered_accounts", return_value={}
            ) as registered:
                account_login_store.private_account_inventory()
                account_login_store.private_account_inventory()
            assert registered.call_count == 1
        finally:
            (
                account_login_store.STATE_PATH,
                account_login_store.LOCK_PATH,
                account_login_store.ACCOUNT_FILES_DIR,
                account_login_store.CONFIG_FILE,
                account_login_store._INVENTORY_CACHE_TTL,
                account_login_store._INVENTORY_CACHE,
            ) = previous


def test_recovery_status_reuses_unchanged_directory_scan():
    previous = recovery_ops._STATUS_CACHE_TTL, recovery_ops._STATUS_CACHE
    recovery_ops._STATUS_CACHE_TTL = 0
    recovery_ops._STATUS_CACHE = None
    try:
        with patch.object(recovery_ops, "find_managed_processes", return_value=[]), patch.object(
            recovery_ops, "_status_input_signature", return_value=("unchanged",)
        ), patch.object(
            recovery_ops, "_records_from_file", return_value={"email:person@example.test": "person@example.test"}
        ) as pending, patch.object(
            recovery_ops, "_account_records", return_value={"email:person@example.test": "person@example.test"}
        ) as accounts, patch.object(
            recovery_ops, "_cpa_emails", return_value=set()
        ) as cpa, patch.object(
            recovery_ops, "_nonempty_line_count", return_value=0
        ), patch.object(recovery_ops, "_read_report", return_value={}):
            first = recovery_ops.recovery_status()
            second = recovery_ops.recovery_status()
        assert first["recoverable_count"] == second["recoverable_count"] == 1
        assert pending.call_count == 1
        assert accounts.call_count == 1
        assert cpa.call_count == 1
    finally:
        recovery_ops._STATUS_CACHE_TTL, recovery_ops._STATUS_CACHE = previous


def test_sso_source_counts_reuse_unchanged_scan():
    previous = sso_state_ops._SOURCE_CACHE_TTL, sso_state_ops._source_cache
    sso_state_ops._SOURCE_CACHE_TTL = 0
    sso_state_ops._source_cache = None
    try:
        with patch.object(
            sso_state_ops, "_source_signature", return_value=("unchanged",)
        ), patch.object(
            sso_state_ops, "_nonempty_line_count", side_effect=[2, 3]
        ) as line_count, patch.object(
            sso_state_ops, "_account_txt_count", return_value=4
        ) as account_count:
            first = sso_state_ops.source_counts()
            second = sso_state_ops.source_counts()
        assert first == second == {"pending": 2, "accounts": 4, "risk": 3}
        assert line_count.call_count == 2
        assert account_count.call_count == 1
    finally:
        sso_state_ops._SOURCE_CACHE_TTL, sso_state_ops._source_cache = previous


def test_bfs_status_reuses_unchanged_jsonl_parse():
    previous = bfs_ops.LOG_DIR, bfs_ops._CACHE_TTL, bfs_ops._RESULT_CACHE
    with tempfile.TemporaryDirectory() as temp:
        bfs_ops.LOG_DIR = Path(temp)
        bfs_ops._CACHE_TTL = 0
        bfs_ops._RESULT_CACHE = None
        (bfs_ops.LOG_DIR / "register_results.jsonl").write_text(
            json.dumps({"status": "ok", "bfs": False}) + "\n",
            encoding="utf-8",
        )
        real_loads = json.loads
        try:
            with patch.object(bfs_ops.json, "loads", wraps=real_loads) as loads:
                first = bfs_ops._jsonl_bfs_from_results()
                second = bfs_ops._jsonl_bfs_from_results()
            assert first == second
            assert loads.call_count == 1
        finally:
            bfs_ops.LOG_DIR, bfs_ops._CACHE_TTL, bfs_ops._RESULT_CACHE = previous


def test_process_status_cache_can_be_bypassed_for_start_checks():
    previous = monitor._PROCESS_CACHE
    monitor._PROCESS_CACHE = None
    processes = [
        {"pid": 101, "pgid": None, "etime": "00:01", "cmd": "python run_until_100.py"}
    ]
    try:
        with patch.object(monitor, "_find_managed_processes", return_value=processes) as find:
            monitor.process_running()
            monitor.process_running()
            monitor.process_running(fresh=True)
        assert find.call_count == 2
    finally:
        monitor._PROCESS_CACHE = previous


def test_panel_pollers_use_single_flight_refreshes():
    source = (ROOT / "webui" / "monitor.py").read_text(encoding="utf-8")
    assert "function refreshOnce(name, task)" in source
    for name in (
        "status",
        "stats",
        "recovery",
        "bfs",
        "sso-state",
        "proxies",
        "email-provider",
        "email-domains",
    ):
        assert f'return refreshOnce("{name}"' in source
    assert "if (accountLoginRefreshPromise) return accountLoginRefreshPromise;" in source


def test_windows_verification_isolates_python_children():
    runner = (ROOT / "scripts" / "run_tests_windows.ps1").read_text(encoding="utf-8")
    contract = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert 'scripts/run_python_isolated.py' in runner
    assert '& $python $test' not in runner
    assert 'Never invoke a test file or Python verification module directly' in contract


if __name__ == "__main__":
    test_account_inventory_reuses_unchanged_parsed_snapshot()
    test_recovery_status_reuses_unchanged_directory_scan()
    test_sso_source_counts_reuse_unchanged_scan()
    test_bfs_status_reuses_unchanged_jsonl_parse()
    test_process_status_cache_can_be_bypassed_for_start_checks()
    test_panel_pollers_use_single_flight_refreshes()
    test_windows_verification_isolates_python_children()
    print("OK monitor performance")

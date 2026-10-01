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
from webui import monitor


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
    test_process_status_cache_can_be_bypassed_for_start_checks()
    test_panel_pollers_use_single_flight_refreshes()
    test_windows_verification_isolates_python_children()
    print("OK monitor performance")

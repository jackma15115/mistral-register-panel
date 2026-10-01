from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import account_sso_match_worker as worker
from webui import account_login_store as store


class FakeRuntime:
    def __init__(
        self,
        accounts_dir: Path,
        resolved_email: str,
        *,
        cpa_ok: bool = True,
        exchange_ok: bool = True,
    ):
        self.accounts_dir = accounts_dir
        self.resolved_email = resolved_email
        self.cpa_ok = cpa_ok
        self.exchange_ok = exchange_ok
        self.retain_failed_sso = None

    def pick_proxy_for_worker(self, _worker_index, _attempt):
        return ""

    def set_thread_proxy(self, _proxy):
        return None

    def clear_thread_proxy(self):
        return None

    def account_file_for_email(self, email):
        return str(self.accounts_dir / f"{email}.txt")

    def add_sso_to_cpa(self, _sso, **kwargs):
        self.retain_failed_sso = kwargs.get("retain_failed_sso")
        if not self.exchange_ok:
            return False
        matched = kwargs["token_callback"](
            {"access_token": "redacted"},
            {"email": self.resolved_email},
        )
        return self.cpa_ok if matched is not False else False


def test_match_worker_preserves_password_and_writes_canonical_account():
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        previous = store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR
        store.STATE_PATH = base / "imported_credentials.json"
        store.LOCK_PATH = base / "imported_credentials.json.lock"
        store.ACCOUNT_FILES_DIR = base / "accounts"
        raw_sso = "s" * 80
        password = "private-password"
        logs = []
        worker.STOP_EVENT.clear()
        try:
            store.import_account_credentials(f"person@example.test----{password}")
            accounts = {item["email"]: item for item in store.private_accounts()}
            runtime = FakeRuntime(base / "accounts", "PERSON@example.test")
            with patch.object(worker, "_log", side_effect=logs.append):
                result = worker.process_one_sso(raw_sso, runtime, accounts, 1)

            assert result["status"] == "success"
            item = store.private_accounts()[0]
            assert item["password"] == password
            assert item["sso"] == raw_sso
            assert item["cpa_ok"] is True
            assert runtime.retain_failed_sso is False
            assert (base / "accounts" / "person@example.test.txt").read_text(
                encoding="utf-8"
            ) == f"person@example.test----{password}----{raw_sso}\n"
            log_text = json.dumps(logs)
            assert raw_sso not in log_text
            assert password not in log_text
            assert "person@example.test" not in log_text
        finally:
            store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR = previous


def test_match_worker_does_not_attach_unknown_email():
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        previous = store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR
        store.STATE_PATH = base / "imported_credentials.json"
        store.LOCK_PATH = base / "imported_credentials.json.lock"
        store.ACCOUNT_FILES_DIR = base / "accounts"
        raw_sso = "t" * 80
        worker.STOP_EVENT.clear()
        try:
            store.import_account_credentials("person@example.test----private-password")
            accounts = {item["email"]: item for item in store.private_accounts()}
            runtime = FakeRuntime(base / "accounts", "unknown@example.test")
            result = worker.process_one_sso(raw_sso, runtime, accounts, 1)

            assert result["status"] == "unmatched"
            item = store.private_accounts()[0]
            assert item["sso"] == ""
            assert not (base / "accounts" / "unknown@example.test.txt").exists()
        finally:
            store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR = previous


def test_private_sso_input_is_removed_after_read():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "private-sso.input"
        raw_sso = "u" * 80
        path.write_text(raw_sso + "\n", encoding="utf-8")
        assert worker._read_private_input(path) == [raw_sso]
        assert not path.exists()


def test_unusable_sso_is_discarded_without_account_changes():
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        previous = store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR
        store.STATE_PATH = base / "imported_credentials.json"
        store.LOCK_PATH = base / "imported_credentials.json.lock"
        store.ACCOUNT_FILES_DIR = base / "accounts"
        raw_sso = "v" * 80
        worker.STOP_EVENT.clear()
        try:
            store.import_account_credentials("person@example.test----private-password")
            accounts = {item["email"]: item for item in store.private_accounts()}
            runtime = FakeRuntime(
                base / "accounts",
                "",
                exchange_ok=False,
            )
            result = worker.process_one_sso(raw_sso, runtime, accounts, 1)

            assert result["status"] == "unusable"
            assert runtime.retain_failed_sso is False
            assert store.private_accounts()[0]["sso"] == ""
            assert not (base / "accounts").exists() or not list(
                (base / "accounts").glob("*.txt")
            )
        finally:
            store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR = previous


if __name__ == "__main__":
    test_match_worker_preserves_password_and_writes_canonical_account()
    test_match_worker_does_not_attach_unknown_email()
    test_private_sso_input_is_removed_after_read()
    test_unusable_sso_is_discarded_without_account_changes()
    print("OK account SSO match worker")

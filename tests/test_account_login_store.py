# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webui import account_login_store as store


def _with_temp_store(temp: str):
    previous = store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR
    store.STATE_PATH = Path(temp) / "imported_credentials.json"
    store.LOCK_PATH = Path(temp) / "imported_credentials.json.lock"
    store.ACCOUNT_FILES_DIR = Path(temp) / "accounts"
    store.ACCOUNT_FILES_DIR.mkdir(parents=True, exist_ok=True)
    return previous


def test_import_formats_and_public_secret_redaction():
    with tempfile.TemporaryDirectory() as temp:
        previous = _with_temp_store(temp)
        try:
            inputs = [
                "one@example.test----Pass-One!",
                "two@example.test,Pass-Two!",
                "three@example.test:Pass:Three!",
                "four@example.test\tPass-Four!",
                'email,passwd\nfive@example.test,"Pass,Five!"',
            ]
            for value in inputs:
                result = store.import_account_credentials(value)
                assert result["ok"] is True

            public = store.read_account_inventory()
            assert public["summary"]["total"] == 5
            encoded = json.dumps(public, ensure_ascii=False)
            for secret in ("Pass-One!", "Pass-Two!", "Pass:Three!", "Pass-Four!", "Pass,Five!"):
                assert secret not in encoded
            assert all(item["has_password"] for item in public["items"])
            assert all("password" not in item and "sso" not in item for item in public["items"])

            private = {item["email"]: item for item in store.private_accounts()}
            assert private["three@example.test"]["password"] == "Pass:Three!"
            assert private["five@example.test"]["password"] == "Pass,Five!"
        finally:
            store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR = previous


def test_deduplicate_update_and_status_changes():
    with tempfile.TemporaryDirectory() as temp:
        previous = _with_temp_store(temp)
        try:
            result = store.import_account_credentials(
                "Same@Example.Test----old-pass\nsame@example.test----new-pass"
            )
            assert result["input_count"] == 1
            assert result["added"] == 1
            item = store.private_accounts()[0]
            assert item["password"] == "new-pass"
            assert store.read_account_inventory()["summary"]["total"] == 1

            store.update_account(
                item["id"],
                status="success",
                sso="secret-sso-value",
                cpa_ok=True,
                last_login_at="2026-07-31T00:00:00Z",
            )
            public = store.read_account_inventory()
            assert public["summary"]["sso_success"] == 1
            assert public["summary"]["cpa_success"] == 1
            assert "secret-sso-value" not in json.dumps(public)

            changed = store.import_account_credentials("same@example.test----replacement")
            assert changed["updated"] == 1
            item = store.private_accounts()[0]
            assert item["status"] == "pending"
            assert item["sso"] == ""
            assert item["cpa_ok"] is False
        finally:
            store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR = previous


def test_import_has_no_account_count_limit():
    text = "\n".join(
        f"person{index}@example.test----password-{index}"
        for index in range(501)
    )
    records = store.parse_account_credentials(text)
    assert len(records) == 501


def test_import_optional_sso_writes_canonical_account_file():
    with tempfile.TemporaryDirectory() as temp:
        previous = _with_temp_store(temp)
        token_one = "s" * 80
        token_two = "t" * 80
        try:
            result = store.import_account_credentials(
                "email,passwd,sso\n"
                f"one@example.test,pass-one,sso=-{token_one}\n"
                f"two@example.test,pass-two,-{token_two}"
            )
            assert result["added"] == 2
            assert result["sso_imported"] == 2
            private = {item["email"]: item for item in store.private_accounts()}
            assert private["one@example.test"]["sso"] == token_one
            assert private["two@example.test"]["sso"] == token_two
            assert (store.ACCOUNT_FILES_DIR / "one@example.test.txt").read_text(
                encoding="utf-8"
            ) == f"one@example.test----pass-one----{token_one}\n"

            public_text = json.dumps(store.read_account_inventory())
            assert token_one not in public_text
            assert token_two not in public_text
            assert "pass-one" not in public_text

            delimited = store.import_account_credentials(
                f"three@example.test----pass-three----{token_one}"
            )
            assert delimited["sso_imported"] == 1
            assert store.private_accounts()[-1]["sso"] == token_one
        finally:
            store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR = previous


def test_reset_incomplete_accounts_is_scoped_to_job_ids():
    with tempfile.TemporaryDirectory() as temp:
        previous = _with_temp_store(temp)
        try:
            store.import_account_credentials(
                "one@example.test----password-one\n"
                "two@example.test----password-two"
            )
            items = store.private_accounts()
            store.mark_accounts_queued([item["id"] for item in items])
            changed = store.reset_incomplete_accounts("worker exited with code 1", [items[0]["id"]])
            assert changed == 1
            after = {item["id"]: item for item in store.private_accounts()}
            assert after[items[0]["id"]]["status"] == "pending"
            assert after[items[0]["id"]]["last_error"] == "worker exited with code 1"
            assert after[items[1]["id"]]["status"] == "queued"
        finally:
            store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR = previous


def test_pending_scopes_split_sso_and_cpa_gaps():
    with tempfile.TemporaryDirectory() as temp:
        previous = _with_temp_store(temp)
        try:
            store.import_account_credentials(
                "missing-sso@example.test----password-one\n"
                "missing-cpa@example.test----password-two\n"
                "complete@example.test----password-three\n"
                "failed-cpa@example.test----password-four"
            )
            items = {item["email"]: item for item in store.private_accounts()}
            store.update_account(items["missing-cpa@example.test"]["id"], sso="sso-two", status="sso_only")
            store.update_account(
                items["complete@example.test"]["id"],
                sso="sso-three",
                status="success",
                cpa_ok=True,
            )
            store.update_account(
                items["failed-cpa@example.test"]["id"],
                sso="sso-four",
                status="failed",
            )

            inventory = store.read_account_inventory()
            assert inventory["summary"]["sso_missing"] == 1
            assert inventory["summary"]["cpa_missing"] == 2
            assert [item["email"] for item in store.private_accounts(pending_scope="sso_missing")] == [
                "missing-sso@example.test"
            ]
            assert {
                item["email"]
                for item in store.private_accounts(pending_scope="cpa_missing")
            } == {"missing-cpa@example.test", "failed-cpa@example.test"}
        finally:
            store.STATE_PATH, store.LOCK_PATH, store.ACCOUNT_FILES_DIR = previous


if __name__ == "__main__":
    test_import_formats_and_public_secret_redaction()
    test_deduplicate_update_and_status_changes()
    test_import_has_no_account_count_limit()
    test_import_optional_sso_writes_canonical_account_file()
    test_reset_incomplete_accounts_is_scoped_to_job_ids()
    print("OK account login store")

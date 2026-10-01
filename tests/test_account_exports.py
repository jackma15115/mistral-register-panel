from __future__ import annotations

import csv
import io
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webui import account_exports, monitor


TOKEN_A = "a" * 80
TOKEN_B = "b" * 80
TOKEN_C = "c" * 80


def _request(url: str, token: str = ""):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=15)
        return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_local_exports_include_pending_sso_and_escape_csv():
    with tempfile.TemporaryDirectory() as temp:
        accounts = Path(temp)
        (accounts / "person@example.test.txt").write_text(
            f"person@example.test----comma,quote\"-----{TOKEN_A}\n",
            encoding="utf-8",
        )
        (accounts / "accounts_batch.txt").write_text(
            f"person@example.test----comma,quote\"-----{TOKEN_A}\n"
            f"PERSON@example.test----new-snapshot----{TOKEN_C}\n",
            encoding="utf-8",
        )
        (accounts / "sso_pending.txt").write_text(
            f"pending@example.test----sso=-{TOKEN_B}\n",
            encoding="utf-8",
        )
        (accounts / "sso_risk_rejected.txt").write_text(
            f"blocked@example.test----{'c' * 80}----policy deny\n",
            encoding="utf-8",
        )
        with patch.object(account_exports, "ACCOUNTS_DIR", accounts):
            sso_name, sso_body = account_exports.sso_export()
            csv_name, csv_body = account_exports.credentials_csv_export()

    assert sso_name.startswith("key-") or sso_name.startswith("grok-register-sso-")
    assert sso_body.decode("utf-8").splitlines() == [TOKEN_A, TOKEN_C, TOKEN_B]
    assert ("-" + TOKEN_A) not in sso_body.decode("utf-8")
    assert csv_name.startswith("accounts-") or csv_name.startswith("grok-register-accounts-")
    assert csv_body.startswith(b"\xef\xbb\xbf")
    parsed = list(csv.reader(io.StringIO(csv_body.decode("utf-8-sig"), newline="")))
    assert parsed == [
        ["email", "passwd", "api_key"],
        ["pending@example.test", "", TOKEN_B],
        ["person@example.test", "comma,quote\"", TOKEN_A],
    ]


def test_export_http_requires_token_and_returns_attachments():
    previous = os.environ.get("MONITOR_TOKEN")
    token = "export-test-token-123456"
    with tempfile.TemporaryDirectory() as temp:
        accounts = Path(temp)
        (accounts / "person@example.test.txt").write_text(
            f"person@example.test----secret-pass----{TOKEN_A}\n",
            encoding="utf-8",
        )
        os.environ["MONITOR_TOKEN"] = token
        with patch.object(account_exports, "ACCOUNTS_DIR", accounts):
            server = monitor.ThreadingHTTPServer(("127.0.0.1", 0), monitor.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                status, _, body = _request(base + "/api/accounts/export-sso")
                assert status == 401
                assert json.loads(body)["ok"] is False

                status, headers, body = _request(
                    base + "/api/accounts/export-sso", token
                )
                assert status == 200
                assert headers["Content-Type"] == "text/plain; charset=utf-8"
                assert "attachment;" in headers["Content-Disposition"]
                assert body.decode("utf-8") == TOKEN_A + "\n"

                status, headers, body = _request(
                    base + "/api/accounts/export-credentials-csv", token
                )
                assert status == 200
                assert headers["Content-Type"] == "text/csv; charset=utf-8"
                assert body.startswith(b"\xef\xbb\xbf")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                if previous is None:
                    os.environ.pop("MONITOR_TOKEN", None)
                else:
                    os.environ["MONITOR_TOKEN"] = previous


if __name__ == "__main__":
    test_local_exports_include_pending_sso_and_escape_csv()
    test_export_http_requires_token_and_returns_attachments()
    print("OK account exports")

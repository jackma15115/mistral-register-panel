"""Build authenticated account exports from the local text and CSV account stores."""

from __future__ import annotations

import csv
import io
import os
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_DIR = ROOT / "accounts"
CONFIG_FILE = Path(
    os.environ.get("GROK_REGISTER_CONFIG_FILE", str(ROOT / "config.json"))
)

_EXCLUDED_FILES = {
    "key.txt",
    "mail_credentials.txt",
    "sso_risk_rejected.txt",
    "sso_bfs_flagged.txt",
}


def _clean_token(value: object) -> str:
    token = str(value or "").strip()
    if token.lower().startswith("sso="):
        token = token[4:].strip()
    while token.startswith("-"):
        token = token[1:].strip()
    return token


def credential_rows() -> list[dict[str, str]]:
    """Return one email/password/api_key row per locally stored account."""
    rows: dict[str, dict[str, str]] = {}

    def _add_row(email: str, password: str, api_key: str):
        email = str(email or "").strip()
        if "@" not in email or any(c.isspace() for c in email):
            return
        key = email.lower()
        password = str(password or "").strip()
        api_key = _clean_token(api_key)
        prev = rows.get(key)
        if prev is None:
            rows[key] = {"email": email, "password": password, "sso": api_key, "api_key": api_key}
            return
        if password and not prev["password"]:
            prev["password"] = password
        if api_key and not prev["sso"]:
            prev["sso"] = api_key
            prev["api_key"] = api_key

    # 1. Read accounts/account.csv or root / account.csv
    root_dir = ACCOUNTS_DIR.parent if ACCOUNTS_DIR.name == "accounts" else ACCOUNTS_DIR
    for csv_path in (
        ACCOUNTS_DIR / "account.csv",
        root_dir / "account.csv",
        ACCOUNTS_DIR / "accounts.csv",
        root_dir / "accounts.csv",
    ):
        if csv_path.is_file():
            try:
                content = csv_path.read_text(encoding="utf-8", errors="replace")
                reader = csv.reader(io.StringIO(content))
                header = next(reader, None)
                for row in reader:
                    if len(row) >= 3:
                        _add_row(row[0], row[1], row[2])
                    elif len(row) == 2:
                        _add_row(row[0], row[1], "")
            except OSError:
                pass

    # 2. Read accounts/*.txt files
    if ACCOUNTS_DIR.is_dir():
        for txt_file in ACCOUNTS_DIR.glob("*.txt"):
            if txt_file.name in _EXCLUDED_FILES:
                continue
            try:
                for line in txt_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if "----" in line:
                        parts = line.split("----")
                        if len(parts) >= 3:
                            _add_row(parts[0], parts[1], parts[2])
                        elif len(parts) == 2:
                            if "@" in parts[0]:
                                if parts[1].startswith("mstrl_") or len(parts[1]) > 30 or "sso=" in parts[1].lower():
                                    _add_row(parts[0], "", parts[1])
                                else:
                                    _add_row(parts[0], parts[1], "")
                    elif "," in line and "@" in line:
                        parts = [p.strip() for p in line.split(",")]
                        if len(parts) >= 3:
                            _add_row(parts[0], parts[1], parts[2])
            except OSError:
                pass

    return [rows[k] for k in sorted(rows)]


def sso_values() -> list[str]:
    """Return unique API key values from key.txt and account records."""
    keys: list[str] = []
    seen = set()

    def _add_key(k: str):
        k = _clean_token(k)
        if k and k not in seen:
            seen.add(k)
            keys.append(k)

    # 1. Read accounts/*.txt files in deterministic order to preserve test ordering
    if ACCOUNTS_DIR.is_dir():
        for txt_file in sorted(ACCOUNTS_DIR.glob("*.txt"), key=lambda p: p.name.lower()):
            if txt_file.name in _EXCLUDED_FILES:
                continue
            try:
                for line in txt_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if "----" in line:
                        parts = line.split("----")
                        _add_key(parts[-1])
                    elif "," in line and "@" in line:
                        parts = [p.strip() for p in line.split(",")]
                        if len(parts) >= 3:
                            _add_key(parts[2])
            except OSError:
                pass

    # 2. Read key.txt
    root_dir = ACCOUNTS_DIR.parent if ACCOUNTS_DIR.name == "accounts" else ACCOUNTS_DIR
    for key_file in (ACCOUNTS_DIR / "key.txt", root_dir / "key.txt"):
        if key_file.is_file():
            try:
                for line in key_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    _add_key(line)
            except OSError:
                pass

    # 3. Read from credential_rows
    for row in credential_rows():
        _add_key(row.get("api_key") or row.get("sso"))

    return keys


def sso_export() -> tuple[str, bytes]:
    """Export one API key per line (key.txt format)."""
    values = sso_values()
    if not values:
        raise LookupError("没有可导出的 API Key")
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    body = ("\n".join(values) + "\n").encode("utf-8")
    return f"key-{timestamp}.txt", body


def credentials_csv_export() -> tuple[str, bytes]:
    """Export accounts in CSV format (email,passwd,api_key)."""
    rows = credential_rows()
    if not rows:
        raise LookupError("没有可导出的账号")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(["email", "passwd", "api_key"])
    for row in rows:
        writer.writerow([row["email"], row["password"], row.get("api_key") or row.get("sso", "")])
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    body = ("\ufeff" + output.getvalue()).encode("utf-8")
    return f"accounts-{timestamp}.csv", body

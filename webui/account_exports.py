"""Build authenticated account exports from the local text account store."""

from __future__ import annotations

import csv
import io
import json
import os
import time
import zipfile
from pathlib import Path

from sso_to_auth_json import load_sso_records


ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_DIR = ROOT / "accounts"
CONFIG_FILE = Path(
    os.environ.get("GROK_REGISTER_CONFIG_FILE", str(ROOT / "config.json"))
)
CPA_AUTH_DIR = (
    Path(os.environ["CPA_AUTH_DIR"])
    if str(os.environ.get("CPA_AUTH_DIR") or "").strip()
    else None
)
GROK2API_AUTH_DIR = (
    Path(os.environ["GROK2API_AUTH_DIR"])
    if str(os.environ.get("GROK2API_AUTH_DIR") or "").strip()
    else None
)

_AUTH_EXPORTS = {
    "cpa": {
        "config_key": "cpa_auth_dir",
        "default_dir": ROOT / "cpa_auth",
        "pattern": "xai-*.json",
        "filename_prefix": "grok-register-cpa-auth",
        "empty_error": "没有可导出的 CPA 凭证",
    },
    "grok2api": {
        "config_key": "grok2api_auth_dir",
        "default_dir": ROOT / "grok2api_auth",
        "pattern": "g2a-*.json",
        "filename_prefix": "grok-register-grok2api-auth",
        "empty_error": "没有可导出的 Grok2API 凭证",
    },
}


def account_records() -> list:
    """Load account and pending-SSO files using the recovery parser."""
    return load_sso_records(
        accounts_dir=str(ACCOUNTS_DIR),
        dedupe_by_email=False,
    )


def sso_values() -> list[str]:
    """Return unique SSO values, including records in ``sso_pending.txt``."""
    return [record.sso for record in account_records() if record.sso]


def credential_rows() -> list[dict[str, str]]:
    """Return one email/password/SSO row per locally stored account."""
    rows: dict[str, dict[str, str]] = {}
    for record in account_records():
        email = str(record.email or "").strip()
        if "@" not in email or any(char.isspace() for char in email):
            continue
        key = email.lower()
        previous = rows.get(key)
        password = str(record.password or "")
        sso = str(record.sso or "")
        if previous is None:
            rows[key] = {"email": email, "password": password, "sso": sso}
            continue
        previous_complete = bool(previous["password"] and previous["sso"])
        current_complete = bool(password and sso)
        if current_complete and not previous_complete:
            rows[key] = {"email": email, "password": password, "sso": sso}
        elif not previous["password"] and password and previous["sso"] == sso:
            previous["password"] = password
    return [rows[key] for key in sorted(rows)]


def sso_export() -> tuple[str, bytes]:
    values = sso_values()
    if not values:
        raise LookupError("没有可导出的 API Key")
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    body = ("\n".join(values) + "\n").encode("utf-8")
    return f"key-{timestamp}.txt", body


def credentials_csv_export() -> tuple[str, bytes]:
    rows = credential_rows()
    if not rows:
        raise LookupError("没有可导出的账号")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(["email", "passwd", "api_key"])
    for row in rows:
        writer.writerow([row["email"], row["password"], row["sso"]])
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    body = ("\ufeff" + output.getvalue()).encode("utf-8")
    return f"accounts-{timestamp}.csv", body


def _auth_export_dir(kind: str) -> Path:
    spec = _AUTH_EXPORTS.get(str(kind or "").strip().lower())
    if spec is None:
        raise ValueError(f"unknown auth export kind: {kind}")

    explicit = CPA_AUTH_DIR if kind == "cpa" else GROK2API_AUTH_DIR
    if explicit is not None:
        return explicit.expanduser().resolve()

    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        config = {}
    raw = str(config.get(spec["config_key"]) or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = CONFIG_FILE.parent / path
        return path.resolve()
    return Path(spec["default_dir"]).resolve()


def auth_files_zip_export(kind: str) -> tuple[str, bytes]:
    """Return a ZIP containing direct, non-symlink auth JSON files."""
    normalized = str(kind or "").strip().lower()
    spec = _AUTH_EXPORTS.get(normalized)
    if spec is None:
        raise ValueError(f"unknown auth export kind: {kind}")
    auth_dir = _auth_export_dir(normalized)
    try:
        paths = sorted(
            (
                path
                for path in auth_dir.glob(spec["pattern"])
                if path.is_file() and not path.is_symlink()
            ),
            key=lambda path: path.name.lower(),
        )
    except OSError:
        paths = []
    if not paths:
        raise LookupError(spec["empty_error"])

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.writestr(path.name, path.read_bytes())
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return f"{spec['filename_prefix']}-{timestamp}.zip", output.getvalue()

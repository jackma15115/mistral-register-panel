"""Private imported-account inventory for browser login jobs."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import copy
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sso_to_auth_json import cpa_auth_filename, grok2api_auth_filename
from sso_utils import normalize_sso_token

try:
    from secure_files import atomic_write_json, atomic_write_text, exclusive_file_lock
    from webui.security_utils import redact_log_line
except ImportError:  # running from webui/
    import sys

    ROOT = Path(__file__).resolve().parent.parent
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from secure_files import atomic_write_json, atomic_write_text, exclusive_file_lock
    from security_utils import redact_log_line  # type: ignore


STATE_PATH = Path(
    os.environ.get(
        "ACCOUNT_LOGIN_STATE_FILE",
        str(ROOT / "accounts" / "imported_credentials.json"),
    )
)
LOCK_PATH = STATE_PATH.with_suffix(STATE_PATH.suffix + ".lock")
ACCOUNT_FILES_DIR = ROOT / "accounts"
CONFIG_FILE = Path(
    os.environ.get("GROK_REGISTER_CONFIG_FILE", str(ROOT / "config.json"))
)

_ACCOUNT_SIDE_FILES = {
    "mail_credentials.txt",
    "sso_pending.txt",
    "sso_risk_rejected.txt",
    "sso_bfs_flagged.txt",
    "mistral_keys.txt",
    "key.txt",
}

MAX_EMAIL_LENGTH = 254
MAX_PASSWORD_LENGTH = 1024
MAX_SSO_LENGTH = 16 * 1024
ALLOWED_STATUSES = {
    "pending",
    "queued",
    "running",
    "success",
    "sso_only",
    "failed",
    "cancelled",
}
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Account files are shared with the registration worker.  A short cache keeps
# the panel responsive while still making newly written accounts visible
# promptly; all local write paths invalidate it immediately.
_INVENTORY_CACHE_TTL = 1.5
_INVENTORY_CACHE_LOCK = threading.Lock()
_INVENTORY_BUILD_LOCK = threading.Lock()
_INVENTORY_CACHE: tuple[tuple[str, str, str], tuple, float, list[dict]] | None = None


def _invalidate_inventory_cache() -> None:
    global _INVENTORY_CACHE
    with _INVENTORY_CACHE_LOCK:
        _INVENTORY_CACHE = None


def _path_signature(path: Path) -> tuple:
    try:
        stat = path.stat()
    except OSError:
        return (str(path), False)
    return (str(path), True, int(stat.st_mtime_ns), int(stat.st_size))


def _inventory_signature() -> tuple:
    account_files = []
    try:
        for path in ACCOUNT_FILES_DIR.glob("*.txt"):
            if path.is_file():
                account_files.append(_path_signature(path))
    except OSError:
        pass
    config = _read_config()
    auth_dirs = (
        _configured_auth_dir("cpa", config),
        _configured_auth_dir("grok2api", config),
    )
    return (
        _path_signature(STATE_PATH),
        _path_signature(CONFIG_FILE),
        tuple(sorted(account_files)),
        tuple(_path_signature(path) for path in auth_dirs),
    )


class AccountImportError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _account_id(email: str) -> str:
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:20]


def _sso_fingerprint(sso: object) -> str:
    value = normalize_sso_token(sso)
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else "missing"


def _clean_text(value: object, limit: int = 240) -> str:
    text = redact_log_line(str(value or ""))
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _normalize_email(value: object) -> str:
    email = str(value or "").strip().lower()
    if not email or len(email) > MAX_EMAIL_LENGTH or not _EMAIL_RE.fullmatch(email):
        raise AccountImportError("invalid email address")
    return email


def _normalize_password(value: object) -> str:
    password = str(value or "").strip()
    if not password:
        raise AccountImportError("password is empty")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AccountImportError("password is too long")
    if "\x00" in password or "\r" in password or "\n" in password:
        raise AccountImportError("password contains unsupported control characters")
    return password


def normalize_sso(value: object, *, required: bool = False) -> str:
    sso = normalize_sso_token(value)
    if not sso:
        if required:
            raise AccountImportError("SSO is empty")
        return ""
    if len(sso) > MAX_SSO_LENGTH:
        raise AccountImportError("SSO is too long")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in sso):
        raise AccountImportError("SSO contains unsupported whitespace or control characters")
    return sso


def parse_sso_values(text: object) -> list[str]:
    lines = [
        line.strip()
        for line in str(text or "").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        raise AccountImportError("no SSO values found")
    values: list[str] = []
    seen: set[str] = set()
    for index, line in enumerate(lines, 1):
        try:
            sso = normalize_sso(line, required=True)
        except AccountImportError as exc:
            raise AccountImportError(f"row {index}: {exc}") from exc
        if sso not in seen:
            seen.add(sso)
            values.append(sso)
    return values


def _parse_csv_with_header(lines: list[str]) -> list[tuple[str, str, str]] | None:
    if not lines or "," not in lines[0]:
        return None
    rows = list(csv.reader(io.StringIO("\n".join(lines))))
    if not rows:
        return None
    header = [str(value or "").strip().lower() for value in rows[0]]
    try:
        email_index = header.index("email")
    except ValueError:
        return None
    password_index = next(
        (header.index(name) for name in ("password", "passwd") if name in header),
        None,
    )
    if password_index is None:
        return None
    sso_index = header.index("sso") if "sso" in header else None
    parsed = []
    for row in rows[1:]:
        if not row or not any(str(value or "").strip() for value in row):
            continue
        if max(email_index, password_index) >= len(row):
            raise AccountImportError("CSV row is missing email or password")
        sso = row[sso_index] if sso_index is not None and sso_index < len(row) else ""
        parsed.append((row[email_index], row[password_index], sso))
    return parsed


def parse_account_credentials(text: object) -> list[tuple[str, str, str]]:
    raw = str(text or "")
    lines = [line for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise AccountImportError("no account credentials found")

    candidates = _parse_csv_with_header(lines)
    if candidates is None:
        candidates = []
        for line_number, line in enumerate(lines, 1):
            value = line.strip()
            if "----" in value:
                parts = value.split("----", 2)
                if len(parts) not in (2, 3):
                    raise AccountImportError(
                        f"line {line_number}: expected email----password[----sso]"
                    )
                email, password = parts[:2]
                sso = parts[2] if len(parts) == 3 else ""
            elif "\t" in value:
                parts = value.split("\t", 2)
                email, password = parts[:2]
                sso = parts[2] if len(parts) == 3 else ""
            elif "," in value:
                row = next(csv.reader([value]))
                if len(row) not in (2, 3):
                    raise AccountImportError(
                        f"line {line_number}: expected email,password[,sso]"
                    )
                email, password = row[:2]
                sso = row[2] if len(row) == 3 else ""
            elif ":" in value:
                email, password = value.split(":", 1)
                sso = ""
            else:
                raise AccountImportError(f"line {line_number}: unsupported account format")
            candidates.append((email, password, sso))

    deduplicated: dict[str, tuple[str, str]] = {}
    for index, (email_value, password_value, sso_value) in enumerate(candidates, 1):
        try:
            email = _normalize_email(email_value)
            password = _normalize_password(password_value)
            sso = normalize_sso(sso_value)
        except AccountImportError as exc:
            raise AccountImportError(f"row {index}: {exc}") from exc
        deduplicated[email] = (password, sso)
    result = [(email, password, sso) for email, (password, sso) in deduplicated.items()]
    if not result:
        raise AccountImportError("no account credentials found")
    return result


def _default_state() -> dict:
    return {"version": 1, "items": [], "updated_at": _utc_now()}


def _normalize_item(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    try:
        email = _normalize_email(raw.get("email"))
        password = _normalize_password(raw.get("password"))
    except AccountImportError:
        return None
    try:
        sso = normalize_sso(raw.get("sso"))
    except AccountImportError:
        sso = ""
    status = str(raw.get("status") or "pending").strip().lower()
    if status not in ALLOWED_STATUSES:
        status = "pending"
    created_at = _clean_text(raw.get("created_at"), 40) or _utc_now()
    return {
        "id": _account_id(email),
        "email": email,
        "password": password,
        "status": status,
        "sso": sso,
        "cpa_ok": bool(raw.get("cpa_ok")),
        "last_error": _clean_text(raw.get("last_error")),
        "created_at": created_at,
        "updated_at": _clean_text(raw.get("updated_at"), 40) or created_at,
        "last_login_at": _clean_text(raw.get("last_login_at"), 40),
    }


def _normalize_state(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("account login state must be an object")
    items_by_email: dict[str, dict] = {}
    for candidate in raw.get("items") or []:
        item = _normalize_item(candidate)
        if item:
            items_by_email[item["email"]] = item
    return {
        "version": 1,
        "items": list(items_by_email.values()),
        "updated_at": _clean_text(raw.get("updated_at"), 40) or _utc_now(),
    }


def _read_unlocked() -> dict:
    if not STATE_PATH.is_file():
        return _default_state()
    try:
        return _normalize_state(json.loads(STATE_PATH.read_text(encoding="utf-8") or "{}"))
    except Exception as exc:
        raise ValueError("imported account state cannot be read; refusing to overwrite it") from exc


def _write_unlocked(state: dict) -> None:
    state["updated_at"] = _utc_now()
    atomic_write_json(STATE_PATH, _normalize_state(state))
    _invalidate_inventory_cache()


def _write_account_file(email: str, password: str, sso: str) -> None:
    atomic_write_text(
        _account_file_path(email),
        f"{email}----{password}----{sso}\n",
    )


def _account_file_path(email: str) -> Path:
    safe_email = str(email or "").replace("/", "_").replace("\\", "_")
    return ACCOUNT_FILES_DIR / f"{safe_email}.txt"


def _read_config() -> dict:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _configured_auth_dir(kind: str, config: dict | None = None) -> Path:
    normalized = str(kind or "").strip().lower()
    if normalized not in {"cpa", "grok2api"}:
        raise ValueError(f"unknown auth directory kind: {kind}")
    env_key = "CPA_AUTH_DIR" if normalized == "cpa" else "GROK2API_AUTH_DIR"
    config_key = "cpa_auth_dir" if normalized == "cpa" else "grok2api_auth_dir"
    default_name = "cpa_auth" if normalized == "cpa" else "grok2api_auth"
    explicit = str(os.environ.get(env_key) or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    current = config if isinstance(config, dict) else _read_config()
    raw = str(current.get(config_key) or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = CONFIG_FILE.parent / path
        return path.resolve()
    return (ROOT / default_name).resolve()


def _auth_file_paths(email: str, config: dict | None = None) -> tuple[Path, Path]:
    current = config if isinstance(config, dict) else _read_config()
    cpa_name = cpa_auth_filename({"email": email})
    g2a_name = grok2api_auth_filename({}, email=email)
    return (
        _configured_auth_dir("cpa", current) / cpa_name,
        _configured_auth_dir("grok2api", current) / g2a_name,
    )


def _is_direct_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _account_file_record(path: Path) -> dict | None:
    if not _is_direct_file(path):
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "----" not in line:
            continue
        parts = line.split("----")
        try:
            email = _normalize_email(parts[0])
        except AccountImportError:
            continue
        if path.name.lower() != _account_file_path(email).name.lower():
            continue
        if len(parts) >= 3:
            password = "----".join(parts[1:-1]).strip()
            raw_sso = parts[-1]
        else:
            password = parts[1].strip()
            raw_sso = ""
        try:
            password = _normalize_password(password)
        except AccountImportError:
            password = ""
        try:
            sso = normalize_sso(raw_sso)
        except AccountImportError:
            sso = ""
        try:
            updated_at = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except OSError:
            updated_at = ""
        return {
            "id": _account_id(email),
            "email": email,
            "password": password,
            "status": "registered",
            "sso": sso,
            "cpa_ok": False,
            "last_error": "",
            "created_at": updated_at,
            "updated_at": updated_at,
            "last_login_at": "",
            "source": "registered",
        }
    return None


def _registered_accounts() -> dict[str, dict]:
    records: dict[str, dict] = {}
    if not ACCOUNT_FILES_DIR.is_dir():
        return records
    try:
        paths = sorted(ACCOUNT_FILES_DIR.glob("*.txt"), key=lambda item: item.name.lower())
    except OSError:
        return records
    for path in paths:
        if path.name in _ACCOUNT_SIDE_FILES or path.name.startswith("accounts_"):
            continue
        record = _account_file_record(path)
        if record:
            records[record["email"]] = record
    return records


def private_account_inventory() -> list[dict]:
    """Return imported and registered accounts with credentials for local workers."""
    global _INVENTORY_CACHE
    cache_key = (str(STATE_PATH), str(ACCOUNT_FILES_DIR), str(CONFIG_FILE))
    now = time.monotonic()
    with _INVENTORY_CACHE_LOCK:
        cached = _INVENTORY_CACHE
        if cached and cached[0] == cache_key and now - cached[2] < _INVENTORY_CACHE_TTL:
            return copy.deepcopy(cached[3])

    # Concurrent panel requests should queue behind one build and then reuse
    # its result instead of scanning every account file independently.
    with _INVENTORY_BUILD_LOCK:
        now = time.monotonic()
        signature = _inventory_signature()
        with _INVENTORY_CACHE_LOCK:
            cached = _INVENTORY_CACHE
            if cached and cached[0] == cache_key and cached[1] == signature:
                _INVENTORY_CACHE = (
                    cache_key,
                    signature,
                    time.monotonic(),
                    cached[3],
                )
                return copy.deepcopy(cached[3])

        with exclusive_file_lock(LOCK_PATH):
            state = _read_unlocked()
        by_email: dict[str, dict] = {}
        for item in state["items"]:
            merged = dict(item)
            merged["source"] = "imported"
            by_email[item["email"]] = merged

        for email, registered in _registered_accounts().items():
            existing = by_email.get(email)
            if existing is None:
                by_email[email] = dict(registered)
                continue
            existing["source"] = "both"
            if not existing.get("password") and registered.get("password"):
                existing["password"] = registered["password"]
            if not existing.get("sso") and registered.get("sso"):
                existing["sso"] = registered["sso"]
            if not existing.get("updated_at") and registered.get("updated_at"):
                existing["updated_at"] = registered["updated_at"]

        config = _read_config()
        for item in by_email.values():
            cpa_path, g2a_path = _auth_file_paths(item["email"], config)
            item["cpa_local"] = _is_direct_file(cpa_path)
            item["grok2api_local"] = _is_direct_file(g2a_path)
            item["cpa_ok"] = bool(item.get("cpa_ok") or item["cpa_local"])
            item["login_eligible"] = item.get("source") in {"imported", "both"}
        result = [by_email[email] for email in sorted(by_email)]
        with _INVENTORY_CACHE_LOCK:
            _INVENTORY_CACHE = (
                cache_key,
                signature,
                time.monotonic(),
                copy.deepcopy(result),
            )
        return result


def _public_item(item: dict) -> dict:
    return {
        "id": item["id"],
        "email": item["email"],
        "status": item["status"],
        "has_password": bool(item.get("password")),
        "has_sso": bool(item.get("sso")),
        "cpa_ok": bool(item.get("cpa_ok")),
        "last_error": item.get("last_error") or "",
        "created_at": item.get("created_at") or "",
        "updated_at": item.get("updated_at") or "",
        "last_login_at": item.get("last_login_at") or "",
        "source": item.get("source") or "imported",
        "cpa_local": bool(item.get("cpa_local")),
        "grok2api_local": bool(item.get("grok2api_local")),
        "login_eligible": bool(item.get("login_eligible", True)),
    }


def read_account_inventory(private_items: list[dict] | None = None) -> dict:
    private_items = private_items if private_items is not None else private_account_inventory()
    items = [_public_item(item) for item in private_items]
    summary = {
        "total": len(items),
        "pending": sum(1 for item in items if item["status"] in {"pending", "cancelled"}),
        "sso_missing": sum(
            1
            for item in items
            if not item["has_sso"] and item["status"] not in {"queued", "running"}
        ),
        "cpa_missing": sum(
            1
            for item in items
            if item["has_sso"] and not item["cpa_ok"] and item["status"] not in {"queued", "running"}
        ),
        "queued": sum(1 for item in items if item["status"] == "queued"),
        "running": sum(1 for item in items if item["status"] == "running"),
        "sso_success": sum(1 for item in items if item["has_sso"]),
        "cpa_success": sum(1 for item in items if item["cpa_ok"]),
        "failed": sum(1 for item in items if item["status"] == "failed"),
    }
    return {"ok": True, "items": items, "summary": summary, "updated_at": _utc_now()}


def import_account_credentials(text: object) -> dict:
    records = parse_account_credentials(text)
    now = _utc_now()
    with exclusive_file_lock(LOCK_PATH):
        state = _read_unlocked()
        by_email = {item["email"]: item for item in state["items"]}
        added = 0
        updated = 0
        unchanged = 0
        sso_imported = sum(1 for _email, _password, sso in records if sso)
        for email, password, sso in records:
            item_id = _account_id(email)
            existing = by_email.get(email)
            if existing is None:
                by_email[email] = {
                    "id": item_id,
                    "email": email,
                    "password": password,
                    "status": "sso_only" if sso else "pending",
                    "sso": sso,
                    "cpa_ok": False,
                    "last_error": "",
                    "created_at": now,
                    "updated_at": now,
                    "last_login_at": "",
                }
                added += 1
            elif existing["password"] != password or (sso and existing["sso"] != sso):
                next_sso = sso if sso else ("" if existing["password"] != password else existing["sso"])
                existing.update(
                    {
                        "password": password,
                        "status": "sso_only" if next_sso else "pending",
                        "sso": next_sso,
                        "cpa_ok": False,
                        "last_error": "",
                        "updated_at": now,
                    }
                )
                updated += 1
            else:
                unchanged += 1
        state["items"] = list(by_email.values())
        _write_unlocked(state)
        for email, password, sso in records:
            if sso:
                _write_account_file(email, password, sso)
    _invalidate_inventory_cache()
    return {
        "ok": True,
        "input_count": len(records),
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "sso_imported": sso_imported,
    }


def attach_sso_by_email(
    email: object,
    sso: object,
    *,
    cpa_ok: bool = False,
    last_error: object = "",
) -> dict | None:
    normalized_email = _normalize_email(email)
    normalized_sso = normalize_sso(sso, required=True)
    now = _utc_now()
    with exclusive_file_lock(LOCK_PATH):
        state = _read_unlocked()
        found = None
        for item in state["items"]:
            if item["email"] != normalized_email:
                continue
            item.update(
                {
                    "sso": normalized_sso,
                    "cpa_ok": bool(cpa_ok),
                    "status": "success" if cpa_ok else "sso_only",
                    "last_error": _clean_text(last_error),
                    "updated_at": now,
                }
            )
            found = dict(item)
            break
        if found is None:
            return None
        _write_unlocked(state)
    return found


def private_accounts(
    ids: object = None,
    *,
    pending_only: bool = False,
    include_sso_only: bool = False,
    pending_scope: str | None = None,
) -> list[dict]:
    requested = {str(value or "").strip() for value in (ids or []) if str(value or "").strip()}
    with exclusive_file_lock(LOCK_PATH):
        state = _read_unlocked()
    result = []
    for item in state["items"]:
        if requested and item["id"] not in requested:
            continue
        if pending_scope == "sso_missing":
            if item["sso"] or item["status"] in {"queued", "running"}:
                continue
        elif pending_scope == "cpa_missing":
            if (
                not item["sso"]
                or item["cpa_ok"]
                or item["status"] in {"queued", "running"}
            ):
                continue
        elif pending_only:
            allowed = {"pending", "failed", "cancelled"}
            if include_sso_only:
                allowed.add("sso_only")
            if item["status"] not in allowed:
                continue
        result.append(dict(item))
    return result


def update_account(account_id: str, **updates: object) -> dict | None:
    allowed = {"status", "sso", "cpa_ok", "last_error", "last_login_at"}
    now = _utc_now()
    with exclusive_file_lock(LOCK_PATH):
        state = _read_unlocked()
        found = None
        for item in state["items"]:
            if item["id"] != str(account_id or ""):
                continue
            for key, value in updates.items():
                if key not in allowed:
                    continue
                if key == "status":
                    status = str(value or "").strip().lower()
                    if status not in ALLOWED_STATUSES:
                        raise ValueError(f"invalid account status: {status}")
                    item[key] = status
                elif key == "cpa_ok":
                    item[key] = bool(value)
                elif key == "last_error":
                    item[key] = _clean_text(value)
                else:
                    item[key] = str(value or "").strip()
            item["updated_at"] = now
            found = dict(item)
            break
        if found is None:
            return None
        _write_unlocked(state)
    return found


def mark_accounts_queued(ids: list[str]) -> int:
    requested = {str(value or "").strip() for value in ids if str(value or "").strip()}
    now = _utc_now()
    changed = 0
    with exclusive_file_lock(LOCK_PATH):
        state = _read_unlocked()
        for item in state["items"]:
            if item["id"] not in requested:
                continue
            item["status"] = "queued"
            item["last_error"] = ""
            item["updated_at"] = now
            changed += 1
        if changed:
            _write_unlocked(state)
    return changed


def reset_incomplete_accounts(
    reason: object = "previous login job was interrupted",
    ids: object = None,
) -> int:
    message = _clean_text(reason) or "previous login job was interrupted"
    requested = (
        {str(value or "").strip() for value in (ids or []) if str(value or "").strip()}
        if ids is not None
        else None
    )
    now = _utc_now()
    changed = 0
    with exclusive_file_lock(LOCK_PATH):
        state = _read_unlocked()
        for item in state["items"]:
            if requested is not None and item["id"] not in requested:
                continue
            if item["status"] not in {"queued", "running"}:
                continue
            item["status"] = "pending"
            item["last_error"] = message
            item["updated_at"] = now
            changed += 1
        if changed:
            _write_unlocked(state)
    return changed


def delete_accounts(ids: object) -> dict:
    requested = {str(value or "").strip() for value in (ids or []) if str(value or "").strip()}
    if not requested:
        raise AccountImportError("no account ids selected")
    with exclusive_file_lock(LOCK_PATH):
        state = _read_unlocked()
        before = len(state["items"])
        state["items"] = [item for item in state["items"] if item["id"] not in requested]
        deleted = before - len(state["items"])
        if deleted:
            _write_unlocked(state)
    return {"ok": True, "deleted": deleted}


def _auth_file_matches_email(path: Path, email: str) -> bool:
    if not _is_direct_file(path):
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    values = [data]
    values.extend(value for value in data.values() if isinstance(value, dict))
    wanted = str(email or "").strip().lower()
    return any(str(value.get("email") or "").strip().lower() == wanted for value in values)


def _resource_paths_for_emails(emails: set[str]) -> list[Path]:
    paths: set[Path] = set()
    for email in emails:
        paths.add(_account_file_path(email))
        paths.update(
            path
            for path in ACCOUNT_FILES_DIR.glob("*.txt")
            if _account_file_record(path) and _account_file_record(path).get("email") in emails
        )
        cpa_path, g2a_path = _auth_file_paths(email)
        paths.add(cpa_path)
        paths.add(g2a_path)
        for auth_dir, pattern in (
            (cpa_path.parent, "xai-*.json"),
            (g2a_path.parent, "g2a-*.json"),
        ):
            try:
                paths.update(
                    path for path in auth_dir.glob(pattern) if _auth_file_matches_email(path, email)
                )
            except OSError:
                pass
    return sorted(paths, key=lambda path: str(path).lower())


def _remove_matching_side_file(path: Path, emails: set[str]) -> int:
    if not _is_direct_file(path):
        return 0
    try:
        with exclusive_file_lock(path.with_suffix(path.suffix + ".lock")):
            lines = path.read_text(encoding="utf-8").splitlines()
            kept: list[str] = []
            removed = 0
            for line in lines:
                raw = line.strip()
                candidate = raw.split("----", 1)[0].split("\t", 1)[0].strip().lower()
                if candidate in emails:
                    removed += 1
                else:
                    kept.append(line)
            if removed:
                body = "\n".join(kept) + ("\n" if kept else "")
                atomic_write_text(path, body)
            return removed
    except (OSError, UnicodeError):
        return 0


def delete_account_resources(
    ids: object,
    *,
    expected_sso_fingerprints: dict[str, str] | None = None,
) -> dict:
    """Delete selected local account data across imported and registered stores."""
    requested = {str(value or "").strip() for value in (ids or []) if str(value or "").strip()}
    if not requested:
        raise AccountImportError("no account ids selected")
    inventory = {item["id"]: item for item in private_account_inventory()}
    selected = [inventory[item_id] for item_id in requested if item_id in inventory]
    if not selected:
        return {"ok": True, "deleted": 0, "removed_files": [], "errors": []}
    emails = {str(item["email"]).strip().lower() for item in selected}
    removed_files: list[str] = []
    errors: list[dict] = []
    expected = expected_sso_fingerprints or {}
    with exclusive_file_lock(LOCK_PATH):
        state = _read_unlocked()
        state_by_id = {item["id"]: item for item in state["items"]}
        registered = _registered_accounts()
        for item in selected:
            expected_fingerprint = str(expected.get(item["id"]) or "")
            if not expected_fingerprint:
                continue
            imported = state_by_id.get(item["id"])
            registered_item = registered.get(item["email"])
            current_sso = str((imported or {}).get("sso") or "")
            if not current_sso:
                current_sso = str((registered_item or {}).get("sso") or "")
            if _sso_fingerprint(current_sso) != expected_fingerprint:
                raise AccountImportError("one or more selected accounts changed after the SSO check")
        before = len(state["items"])
        state["items"] = [item for item in state["items"] if item["email"] not in emails]
        if len(state["items"]) != before:
            _write_unlocked(state)

    for side_name in ("sso_pending.txt", "sso_risk_rejected.txt", "sso_bfs_flagged.txt", "mail_credentials.txt"):
        side_path = ACCOUNT_FILES_DIR / side_name
        _remove_matching_side_file(side_path, emails)

    for path in _resource_paths_for_emails(emails):
        if not _is_direct_file(path):
            continue
        try:
            path.unlink()
            removed_files.append(str(path))
        except OSError as exc:
            errors.append({"path": str(path), "error": _clean_text(exc)})
    _invalidate_inventory_cache()
    return {
        "ok": not errors,
        "deleted": len(selected),
        "emails": len(emails),
        "removed_files": removed_files,
        "errors": errors,
    }

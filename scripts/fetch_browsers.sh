#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${BROWSER_PYTHON:-python}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/resolve_browser_cache.sh"
apply_browser_cache_env "${REPO_DIR}"

camoufox_ready() {
  "${PYTHON_BIN}" - <<'PY' >/dev/null 2>&1
from pathlib import Path
from camoufox.pkgman import INSTALL_DIR, Version

root = Path(INSTALL_DIR)
if not root.is_dir() or not any(root.iterdir()):
    raise SystemExit(1)
try:
    raise SystemExit(0 if Version.from_path().is_supported() else 2)
except Exception:
    raise SystemExit(3)
PY
}

echo "[fetch-browsers] cache=${XDG_CACHE_HOME}"
if camoufox_ready; then
  echo "[fetch-browsers] Camoufox already present"
else
  echo "[fetch-browsers] downloading Camoufox selected by the installed package"
  "${PYTHON_BIN}" -m camoufox fetch
fi

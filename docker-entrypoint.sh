#!/usr/bin/env bash
set -euo pipefail

umask 077

APP_ROOT="/app"
DATA_ROOT="${GROK_REGISTER_DATA_DIR:-/data}"

case "${DATA_ROOT}" in
  /*) ;;
  *)
    echo "[entrypoint] GROK_REGISTER_DATA_DIR must be an absolute path" >&2
    exit 1
    ;;
esac

mkdir -p \
  "${DATA_ROOT}/accounts" \
  "${DATA_ROOT}/cpa_auth" \
  "${DATA_ROOT}/grok2api_auth" \
  "${DATA_ROOT}/log" \
  "${DATA_ROOT}/cache"

if ! touch "${DATA_ROOT}/.write-test" 2>/dev/null; then
  echo "[entrypoint] data directory is not writable: ${DATA_ROOT}" >&2
  exit 1
fi
rm -f "${DATA_ROOT}/.write-test"

CONFIG_FILE="${GROK_REGISTER_CONFIG_FILE:-${DATA_ROOT}/config.json}"
if [[ ! -f "${CONFIG_FILE}" ]]; then
  mkdir -p "$(dirname "${CONFIG_FILE}")"
  cp "${APP_ROOT}/config.example.json" "${CONFIG_FILE}"
  echo "[entrypoint] initialized ${CONFIG_FILE} from config.example.json"
fi
chmod 600 "${CONFIG_FILE}" 2>/dev/null || true

link_runtime_dir() {
  local name="$1"
  local source_path="${DATA_ROOT}/${name}"
  local app_path="${APP_ROOT}/${name}"
  mkdir -p "${source_path}"
  rm -rf -- "${app_path}"
  ln -s "${source_path}" "${app_path}"
}

for runtime_dir in accounts cpa_auth grok2api_auth log; do
  link_runtime_dir "${runtime_dir}"
done

# Legacy proxy-file users can place /data/proxies.txt without rebuilding.
rm -f "${APP_ROOT}/proxies.txt"
ln -s "${DATA_ROOT}/proxies.txt" "${APP_ROOT}/proxies.txt"

export GROK_REGISTER_CONFIG_FILE="${CONFIG_FILE}"
export EMAIL_PROVIDER_CONFIG_FILE="${EMAIL_PROVIDER_CONFIG_FILE:-${CONFIG_FILE}}"
export CPA_AUTH_DIR="${CPA_AUTH_DIR:-${DATA_ROOT}/cpa_auth}"
export PROXY_POOL_STATE_FILE="${PROXY_POOL_STATE_FILE:-${DATA_ROOT}/log/proxy_pool.json}"
export PROXY_POOL_LEGACY_FILE="${PROXY_POOL_LEGACY_FILE:-${DATA_ROOT}/proxies.txt}"
export EMAIL_DOMAIN_POOL_STATE_FILE="${EMAIL_DOMAIN_POOL_STATE_FILE:-${DATA_ROOT}/log/email_domain_pool.json}"
export BLACKLIST_STATE_FILE="${BLACKLIST_STATE_FILE:-${DATA_ROOT}/log/blacklist_state.json}"
export ACCOUNT_LOGIN_STATE_FILE="${ACCOUNT_LOGIN_STATE_FILE:-${DATA_ROOT}/accounts/imported_credentials.json}"
export ACCOUNT_LOGIN_JOB_FILE="${ACCOUNT_LOGIN_JOB_FILE:-${DATA_ROOT}/log/account_login_job.json}"
export ACCOUNT_LOGIN_REPORT_FILE="${ACCOUNT_LOGIN_REPORT_FILE:-${DATA_ROOT}/log/account_login_report.json}"
export ACCOUNT_LOGIN_PID_FILE="${ACCOUNT_LOGIN_PID_FILE:-${DATA_ROOT}/log/account_login.pid}"
export NEXT_ACTION_CACHE_FILE="${NEXT_ACTION_CACHE_FILE:-${DATA_ROOT}/.next_action_id.cache}"

# shellcheck disable=SC1091
source "${APP_ROOT}/scripts/resolve_browser_cache.sh"
apply_browser_cache_env "${APP_ROOT}"
echo "[entrypoint] data=${DATA_ROOT} config=${CONFIG_FILE} browser_cache=${XDG_CACHE_HOME}"

auto_fetch="$(printf '%s' "${GROK_REGISTER_BROWSER_AUTO_FETCH:-1}" | tr '[:upper:]' '[:lower:]')"
if [[ "${auto_fetch}" != "0" && "${auto_fetch}" != "false" && "${auto_fetch}" != "no" && "${auto_fetch}" != "off" ]]; then
  BROWSER_PYTHON="${APP_ROOT}/.venv/bin/python" \
    "${APP_ROOT}/scripts/fetch_browsers.sh"
fi

monitor_host="${MONITOR_HOST:-0.0.0.0}"
app_command=" $* "
if [[ "${app_command}" == *" webui/monitor.py "* \
  && "${monitor_host}" != "127.0.0.1" \
  && "${monitor_host}" != "localhost" \
  && "${monitor_host}" != "::1" \
  && -z "${MONITOR_TOKEN:-}" ]]; then
  echo "[entrypoint] MONITOR_TOKEN is required when the panel is exposed outside loopback" >&2
  exit 1
fi

exec "$@"

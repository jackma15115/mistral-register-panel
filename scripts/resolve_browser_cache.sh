#!/usr/bin/env bash
# Resolve one Camoufox cache for image-baked and persistent runtime installs.

_has_camoufox() {
  local root="$1"
  [[ -n "${root}" && -d "${root}/camoufox" ]] || return 1
  compgen -G "${root}/camoufox/*" >/dev/null 2>&1
}

resolve_browser_cache() {
  local repo_dir="${1:-}"
  local data_dir="${GROK_REGISTER_DATA_DIR:-/data}"

  if _has_camoufox "${XDG_CACHE_HOME:-}"; then
    printf '%s\n' "${XDG_CACHE_HOME}"
  elif _has_camoufox "/opt/browser-cache"; then
    printf '%s\n' "/opt/browser-cache"
  elif _has_camoufox "${data_dir}/cache" || [[ -d "${data_dir}" ]]; then
    printf '%s\n' "${data_dir}/cache"
  elif [[ -n "${repo_dir}" ]]; then
    printf '%s\n' "${repo_dir}/.cache"
  else
    printf '%s\n' "${HOME:-/root}/.cache"
  fi
}

apply_browser_cache_env() {
  local repo_dir="${1:-}"
  XDG_CACHE_HOME="$(resolve_browser_cache "${repo_dir}")"
  PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-${XDG_CACHE_HOME}/ms-playwright}"
  export XDG_CACHE_HOME PLAYWRIGHT_BROWSERS_PATH
  mkdir -p "${XDG_CACHE_HOME}" "${PLAYWRIGHT_BROWSERS_PATH}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  repo="$(cd "$(dirname "$0")/.." && pwd)"
  apply_browser_cache_env "${repo}"
  printf '%s\n' "${XDG_CACHE_HOME}"
fi

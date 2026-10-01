#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE="${GROK_REGISTER_IMAGE:-}"
VERSION="${VERSION:-$(tr -d '[:space:]' < VERSION)}"
PLATFORMS="${DOCKER_PLATFORMS:-linux/amd64}"

if [[ -z "${IMAGE}" || "${IMAGE}" == "grok-register-panel:local" ]]; then
  echo "Set GROK_REGISTER_IMAGE to a registry image, for example registry.example.com/team/grok-register-panel" >&2
  exit 1
fi
if ! docker buildx version >/dev/null 2>&1; then
  echo "docker buildx is required" >&2
  exit 1
fi

for required_rule in '.env' 'config.json' 'accounts/' 'recovered_source/'; do
  if ! grep -qF "${required_rule}" .dockerignore; then
    echo ".dockerignore is missing required privacy rule: ${required_rule}" >&2
    exit 1
  fi
done

version_tag="${IMAGE}:${VERSION}"
latest_tag="${IMAGE}:latest"
echo "[publish] ${version_tag} platforms=${PLATFORMS}"
docker buildx build \
  --pull \
  --platform "${PLATFORMS}" \
  --tag "${version_tag}" \
  --tag "${latest_tag}" \
  --push \
  .

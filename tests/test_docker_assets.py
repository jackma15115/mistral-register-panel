#!/usr/bin/env python3
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_docker_image_contract() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    example_config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))

    assert "python:3.12-slim-bookworm" in dockerfile
    assert "python -m camoufox fetch" in dockerfile
    assert 'ENTRYPOINT ["tini", "-g"' in dockerfile
    assert 'CMD ["python", "-u", "webui/monitor.py"]' in dockerfile
    assert 'VOLUME ["/data"]' in dockerfile
    assert "/api/health" in dockerfile
    assert "libtk8.6" in dockerfile
    assert "nodejs" in dockerfile
    assert "import tkinter" in dockerfile
    assert example_config["proxy"] == ""

    assert "MONITOR_TOKEN: \"${MONITOR_TOKEN:?" in compose
    assert "MONITOR_BIND_ADDRESS:-127.0.0.1" in compose
    assert "DOCKER_PLATFORM:-linux/amd64" in compose
    assert "seccomp:unconfined" in compose
    assert "shm_size: \"1gb\"" in compose
    assert ":/data" in compose
    assert 'MONITOR_HOST: "0.0.0.0"' in compose
    assert "MONITOR_MAX_REQUEST_BODY" in compose

    for runtime_dir in ("accounts", "cpa_auth", "grok2api_auth", "log"):
        assert runtime_dir in entrypoint
    assert "GROK_REGISTER_CONFIG_FILE" in entrypoint
    assert "ACCOUNT_LOGIN_STATE_FILE" in entrypoint
    assert "NEXT_ACTION_CACHE_FILE" in entrypoint
    assert "MONITOR_TOKEN is required" in entrypoint
    assert 'app_command=" $* "' in entrypoint
    assert "scripts/fetch_browsers.sh" in entrypoint

    for private_path in (
        ".env",
        "config.json",
        "accounts/",
        "cpa_auth/",
        "grok2api_auth/",
        "log/",
        "recovered_source/",
        "*.pem",
        "*.sqlite3",
    ):
        assert private_path in dockerignore


def test_runtime_paths_are_container_overridable() -> None:
    worker = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    batch = (ROOT / "run_batch_headless.py").read_text(encoding="utf-8")
    monitor = (ROOT / "webui" / "monitor.py").read_text(encoding="utf-8")
    login = (ROOT / "webui" / "account_login_ops.py").read_text(encoding="utf-8")

    for source in (worker, batch, monitor):
        assert "GROK_REGISTER_CONFIG_FILE" in source
    assert "ACCOUNT_LOGIN_STATE_FILE" in (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")


def test_docker_publish_workflow() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "docker-publish.yml"
    ).read_text(encoding="utf-8")

    assert 'tags:' in workflow
    assert '"v*"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "packages: write" in workflow
    assert "ghcr.io/${{ github.repository }}" in workflow
    assert "docker/login-action@v3" in workflow
    assert "docker/metadata-action@v5" in workflow
    assert "docker/setup-qemu-action@v3" in workflow
    assert "docker/build-push-action@v6" in workflow
    assert "platforms: linux/amd64,linux/arm64" in workflow
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    assert "cache-to: type=gha,mode=max" in workflow


if __name__ == "__main__":
    test_docker_image_contract()
    test_runtime_paths_are_container_overridable()
    test_docker_publish_workflow()
    print("OK docker assets")

#!/usr/bin/env python3
from __future__ import annotations

import stat
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import batch_supervisor
import browser_session
import grok_register_ttk


def test_windows_profile_root_uses_local_app_data():
    with tempfile.TemporaryDirectory() as temp:
        root = browser_session._profile_root(
            platform_name="nt",
            environ={"LOCALAPPDATA": temp},
        )
        assert root.resolve() == (
            Path(temp) / "GrokRegister" / "grok-register-camoufox"
        ).resolve()
        assert root.is_dir()
        if sys.platform != "win32":
            assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert browser_session._is_managed_profile_dir(
            str(root / "123-456-abcdef12")
        )


def test_proxy_ip_validation_is_strict():
    assert browser_session._normalize_ip_candidate("203.0.113.8\n") == "203.0.113.8"
    assert browser_session._normalize_ip_candidate("2001:db8::1") == "2001:db8::1"
    assert browser_session._normalize_ip_candidate("999.999.999.999") == ""
    assert browser_session._normalize_ip_candidate("not-an-ip") == ""


def test_windows_does_not_select_posix_playwright_wrapper():
    wrapper = str(ROOT / "scripts" / "playwright-node")
    previous = browser_session.os.environ.get("PLAYWRIGHT_NODEJS_PATH")
    browser_session.os.environ["PLAYWRIGHT_NODEJS_PATH"] = wrapper
    try:
        browser_session._pin_playwright_node(platform_name="nt")
        assert "PLAYWRIGHT_NODEJS_PATH" not in browser_session.os.environ
    finally:
        if previous is None:
            browser_session.os.environ.pop("PLAYWRIGHT_NODEJS_PATH", None)
        else:
            browser_session.os.environ["PLAYWRIGHT_NODEJS_PATH"] = previous


def test_linux_without_system_node_uses_playwright_bundled_node():
    wrapper = str(ROOT / "scripts" / "playwright-node")
    previous_env = browser_session.os.environ.get("PLAYWRIGHT_NODEJS_PATH")
    previous_isfile = browser_session.os.path.isfile
    previous_which = browser_session.shutil.which

    def fake_isfile(path):
        if str(path) in {"/usr/bin/node", "/usr/local/bin/node"}:
            return False
        return previous_isfile(path)

    browser_session.os.environ["PLAYWRIGHT_NODEJS_PATH"] = wrapper
    browser_session.os.path.isfile = fake_isfile
    browser_session.shutil.which = lambda _name: None
    try:
        browser_session._pin_playwright_node(platform_name="posix")
        assert "PLAYWRIGHT_NODEJS_PATH" not in browser_session.os.environ
    finally:
        browser_session.os.path.isfile = previous_isfile
        browser_session.shutil.which = previous_which
        if previous_env is None:
            browser_session.os.environ.pop("PLAYWRIGHT_NODEJS_PATH", None)
        else:
            browser_session.os.environ["PLAYWRIGHT_NODEJS_PATH"] = previous_env


def test_account_gap_sleep_is_cancelable():
    started = time.monotonic()
    grok_register_ttk._sleep_cancelable(2, lambda: True)
    assert time.monotonic() - started < 0.5


class FakePsutilError(Exception):
    pass


class FakeTrackedProcess:
    def __init__(self, pid):
        self.pid = pid
        self.terminated = False
        self.killed = False
        self._children = []

    def children(self, recursive=False):
        assert recursive is True
        return list(self._children)

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class FakePopen:
    def __init__(self, pid):
        self.pid = pid
        self.terminated = False
        self.killed = False

    def send_signal(self, _signal):
        pass

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class FakePsutil:
    Error = FakePsutilError

    def __init__(self):
        self.child = FakeTrackedProcess(102)
        self.root = FakeTrackedProcess(101)
        self.root._children = [self.child]
        self.wait_calls = 0

    def Process(self, pid):
        assert pid == 101
        return self.root

    def wait_procs(self, processes, timeout):
        assert timeout > 0
        self.wait_calls += 1
        return [], list(processes)


def test_windows_process_tree_terminates_descendants():
    fake_psutil = FakePsutil()
    batch_supervisor._terminate_windows_process_tree(
        FakePopen(101),
        grace_seconds=0.2,
        psutil_module=fake_psutil,
    )
    assert fake_psutil.root.terminated and fake_psutil.child.terminated
    assert fake_psutil.root.killed and fake_psutil.child.killed
    source = (ROOT / "batch_supervisor.py").read_text(encoding="utf-8")
    assert "selectors.DefaultSelector" not in source
    assert "target=_read_pipe" in source


if __name__ == "__main__":
    test_windows_profile_root_uses_local_app_data()
    test_proxy_ip_validation_is_strict()
    test_windows_does_not_select_posix_playwright_wrapper()
    test_linux_without_system_node_uses_playwright_bundled_node()
    test_account_gap_sleep_is_cancelable()
    test_windows_process_tree_terminates_descendants()
    print("OK windows runtime")

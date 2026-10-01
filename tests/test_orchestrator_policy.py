#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_until_100 as orch
from retry_policy import PRECHECK_EXIT_CODE


class FakeProcess:
    def __init__(self, pid: int, return_code: int):
        self.pid = pid
        self.return_code = return_code

    def poll(self):
        return self.return_code


def _run_with_exit_codes(
    exit_codes, *, target=1, batch_count=40, advance_on_start=False
):
    names = (
        "apply_control",
        "kill_batch",
        "cpa_count",
        "read_blocklist_asns",
        "start_batch",
        "batch_alive",
        "count_risk",
        "count_ok",
        "analyze_risks_and_expand",
        "orchestrator_failure_limit",
        "log",
    )
    previous = {name: getattr(orch, name) for name in names}
    previous_base = orch.BASE0
    previous_target = orch.TARGET_CPA
    previous_batch_count = orch.BATCH_COUNT
    launches = []
    launch_counts = []
    messages = []
    codes = iter(exit_codes)
    state = {"cpa": 0}
    try:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "batch.log"
            log_path.write_text("", encoding="utf-8")
            orch.BASE0 = 0
            orch.TARGET_CPA = target
            orch.BATCH_COUNT = batch_count
            orch.apply_control = lambda: None
            orch.kill_batch = lambda: None
            orch.cpa_count = lambda: state["cpa"]
            orch.read_blocklist_asns = lambda: set()
            orch.batch_alive = lambda _pid: False
            orch.count_risk = lambda _path: 0
            orch.count_ok = lambda _path: 0
            orch.analyze_risks_and_expand = lambda _path: []
            orch.orchestrator_failure_limit = lambda: 2
            orch.log = messages.append

            def start_batch(count):
                code = next(codes)
                launches.append(code)
                launch_counts.append(count)
                if advance_on_start:
                    state["cpa"] += count
                return FakeProcess(100 + len(launches), code), log_path

            orch.start_batch = start_batch
            original_sleep = orch.time.sleep
            orch.time.sleep = lambda _seconds: None
            try:
                orch.main()
            finally:
                orch.time.sleep = original_sleep
    finally:
        for name, value in previous.items():
            setattr(orch, name, value)
        orch.BASE0 = previous_base
        orch.TARGET_CPA = previous_target
        orch.BATCH_COUNT = previous_batch_count
    return launches, launch_counts, messages


def test_apply_control_loads_unlimited_batch_count():
    previous_control_file = orch.CONTROL_FILE
    previous_auths = orch.AUTHS
    previous_batch_count = orch.BATCH_COUNT
    previous_base = orch.BASE0
    previous_target = orch.TARGET_CPA
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        orch.CONTROL_FILE = root / "monitor_control.json"
        orch.AUTHS = root / "cpa_auth"
        orch.AUTHS.mkdir()
        orch.CONTROL_FILE.write_text(
            '{"batch_count": 250000, "add_count": 10}', encoding="utf-8"
        )
        try:
            orch.apply_control()
            assert orch.BATCH_COUNT == 250_000
            assert orch.TARGET_CPA == 10
        finally:
            orch.CONTROL_FILE = previous_control_file
            orch.AUTHS = previous_auths
            orch.BATCH_COUNT = previous_batch_count
            orch.BASE0 = previous_base
            orch.TARGET_CPA = previous_target


def test_orchestrator_uses_configured_batch_count_and_caps_to_remaining():
    _, launch_counts, _ = _run_with_exit_codes(
        [PRECHECK_EXIT_CODE], target=75, batch_count=30
    )
    assert launch_counts == [30]

    _, launch_counts, _ = _run_with_exit_codes(
        [PRECHECK_EXIT_CODE], target=12, batch_count=30
    )
    assert launch_counts == [12]


def test_orchestrator_has_no_total_round_limit():
    launches, launch_counts, _ = _run_with_exit_codes(
        [0] * 61,
        target=61,
        batch_count=1,
        advance_on_start=True,
    )
    assert len(launches) == 61
    assert launch_counts == [1] * 61


def test_precheck_failure_stops_orchestrator_immediately():
    launches, _, messages = _run_with_exit_codes([PRECHECK_EXIT_CODE])
    assert launches == [PRECHECK_EXIT_CODE]
    assert any("precheck failed" in message for message in messages)


def test_consecutive_abnormal_batches_are_bounded():
    launches, _, messages = _run_with_exit_codes([1, 1, 1])
    assert launches == [1, 1]
    assert any("consecutive batch failures=2/2" in message for message in messages)


if __name__ == "__main__":
    test_apply_control_loads_unlimited_batch_count()
    test_orchestrator_uses_configured_batch_count_and_caps_to_remaining()
    test_orchestrator_has_no_total_round_limit()
    test_precheck_failure_stops_orchestrator_immediately()
    test_consecutive_abnormal_batches_are_bounded()
    print("OK orchestrator policy")

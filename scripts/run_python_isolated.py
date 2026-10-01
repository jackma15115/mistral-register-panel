#!/usr/bin/env python3
"""Run one Python verification command without sharing the caller's console."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("python_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    python_args = list(args.python_args)
    if python_args[:1] == ["--"]:
        python_args = python_args[1:]
    if not python_args:
        parser.error("a Python script or module command is required")

    creationflags = 0
    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )

    command = [sys.executable, *python_args]
    try:
        completed = subprocess.run(
            command,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, args.timeout),
            creationflags=creationflags,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        if exc.stdout:
            sys.stdout.write(str(exc.stdout))
        if exc.stderr:
            sys.stderr.write(str(exc.stderr))
        sys.stderr.write(f"isolated verification timed out after {args.timeout:g}s\n")
        return 124

    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Convenience wrapper for the Python bench prototype."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))

from ai_hardware_bench.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())


from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from greeter import greet


def test_greet_with_name() -> None:
    assert greet("Ana") == "Hello, Ana!"


def test_greet_with_blank_name() -> None:
    assert greet("   ") == "Hello, world!"

from __future__ import annotations

from scripts.regenerate_golden import main


def test_twenty_golden_bookcase_fixtures_match() -> None:
    import sys

    original = sys.argv
    try:
        sys.argv = ["regenerate_golden.py", "--check"]
        assert main() == 0
    finally:
        sys.argv = original

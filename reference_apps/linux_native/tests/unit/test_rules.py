import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ticket_rules import valid_title


def test_required_title():
    assert not valid_title("   ")
    assert valid_title("real")


def test_length_limit():
    assert valid_title("x" * 120)
    assert not valid_title("x" * 121)

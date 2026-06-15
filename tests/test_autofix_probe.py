"""Regression tests for api/_autofix_probe.divide_safely's documented contract."""
from api._autofix_probe import divide_safely


def test_divide_safely_returns_quotient():
    assert divide_safely(10, 2) == 5


def test_divide_safely_returns_none_on_zero_divisor():
    # Documented contract: None instead of ZeroDivisionError.
    assert divide_safely(1, 0) is None

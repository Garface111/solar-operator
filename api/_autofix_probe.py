"""Throwaway module for auto-fix end-to-end verification. Safe to delete.
Contains a deliberate bug: divide_safely crashes on divisor=0 instead of
returning None as its docstring promises.
"""
from __future__ import annotations


def divide_safely(a: float, b: float):
    """Return a / b, or None when b is 0 (so callers never hit ZeroDivisionError)."""
    return a / b

"""All-time aggregated fleet reporting (Array Operator).

This package builds the operator's whole-fleet, all-time aggregated data
report — generation by year/month and per-array — read LIVE from the DB on
every call so it always reflects the latest absorbed month (no frozen
snapshot). See api/reports/fleet_report.py.
"""

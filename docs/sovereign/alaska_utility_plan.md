# Alaska Utility Adapter Plan

**Request**: alaska (state=AK, url=-)

**Status**: Research only — no evidence of SmartHub host or login flow.

**Next steps (no code change)**:
- Query AK.csv providers for smarthub_host column.
- If any match *.smarthub.coop, add to SMARTHUB_UTILITIES via CSV (edit api/adapters/smarthub.py only after evidence).
- If bespoke: capture real HAR; draft adapter plan only.
- Do not promote to 'added' or invent endpoints.

**Evidence required**: Live login trace or confirmed SmartHub subdomain before any adapter edit.
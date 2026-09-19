> Synthetic demonstration only. These utility assignments and counts are not Norwich data.

# Utility integration triage

4 source groups; 300 supplied offtaker assignments.
**Planning only. No group has been qualified for automatic invoicing by this report.**

Counts sum the supplied groups. Keep groups disjoint; this tool cannot establish unique customers without Norwich's roster.

| Group | Utility | Offtakers | Family | Billing source | Work required |
|---|---|---:|---|---|---|
| example-gmp | Green Mountain Power (GMP) | 100 | gmp | bill_credit | qualify_existing_family |
| example-smarthub | Vermont Electric Cooperative | 80 | smarthub | production_kwh | qualify_existing_family |
| example-unknown | Fictional Example Utility | 70 | unknown | bill_credit | new_connector |
| example-cmp | Central Maine Power (CMP) | 50 | cmp | allocation_statement | add_billing_evidence_path |

## example-gmp

Confirm the actual account, generation meter, credit lines and contract rate.

Required evidence:

- official_utility_and_portal_identity
- authorized_access_and_mfa_recovery
- account_meter_and_offtaker_mapping
- two_closed_cycles_with_original_source_files
- independently_checked_amounts_units_and_contract
- relogin_and_repeat_capture
- incomplete_wrong_account_and_duplicate_rejection
- reviewed_live_invoice_canary

## example-smarthub

Qualify each utility and meter. Bill-credit mode requires actual statement credit/excess semantics and a verified parser. Usage, export and prorated bill totals are not measured generation. Monthly only until quarterly support is implemented.

Required evidence:

- official_utility_and_portal_identity
- authorized_access_and_mfa_recovery
- account_meter_and_offtaker_mapping
- two_closed_cycles_with_original_source_files
- independently_checked_amounts_units_and_contract
- relogin_and_repeat_capture
- incomplete_wrong_account_and_duplicate_rejection
- reviewed_live_invoice_canary

## example-unknown

Discover the official portal and source contract before building or enabling a connector.

Required evidence:

- official_utility_and_portal_identity
- authorized_access_and_mfa_recovery
- account_meter_and_offtaker_mapping
- two_closed_cycles_with_original_source_files
- independently_checked_amounts_units_and_contract
- relogin_and_repeat_capture
- incomplete_wrong_account_and_duplicate_rejection
- reviewed_live_invoice_canary

## example-cmp

The current collector emits no bill records. Community allocation statements require a separate verified source and mapping.

Required evidence:

- official_utility_and_portal_identity
- authorized_access_and_mfa_recovery
- account_meter_and_offtaker_mapping
- two_closed_cycles_with_original_source_files
- independently_checked_amounts_units_and_contract
- relogin_and_repeat_capture
- incomplete_wrong_account_and_duplicate_rejection
- reviewed_live_invoice_canary

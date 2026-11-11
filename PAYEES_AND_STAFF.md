# Payees and staff

The existing `vendors` table is the shared payee directory. Records keep their
IDs and transaction links. `roles` contains one or more of `SUPPLIER`,
`SUBCONTRACTOR`, and `STAFF`. The existing `role` string remains the job title
or work description, and `category` remains the default expense category.

Staff appear in the Staff screen. Payees with Supplier or Subcontractor roles
appear in Vendors; a person with multiple roles can appear in both screens.
Legacy records without `roles` remain readable through category-based inference.
Only explicit STAFF categories are inferred as staff.

Salary payments are EXPENSE transactions with category `Staff Salaries`, the
staff payee ID in `vendor`, and `salary_month` in YYYY-MM format. Payment date
and salary month are independent. New salary payments require a month and a
payee with the Staff role. An employee with salary history cannot lose that
role. Historical payments may have no salary month; the UI displays Not recorded
and requires the month when editing a salary payment. Payment analytics continue
to use payment dates. Reversed salary payments are excluded from staff totals.

No new DynamoDB tables or indexes are required for this release. Query filtering
and pagination still operate on a workspace snapshot; database-level paging and
date/payee access indexes remain a separate scaling change.

## A&T INFRA migration status

Applied and read-back verified on 10 November 2026, ledger revision 43 to 44:
12 payees now have explicit roles (one staff member, three suppliers and eight
subcontractors). Ten active expense payments were reclassified: two for
RAJU EEDGI, six for GD Saw Mill and two for Sri Karthikeya. The two salary months
remain unrecorded. Live GET checks through all three deployed handlers confirmed
the classifications and the unchanged expense total. The backup and receipt
are under `.local/payee-role-migration/`, with the receipt named
`20261110T013029496230-receipt.json`. Previous Lambda packages are retained under
`.local/deploy/staff-release-backups/`.

## Existing-data migration

Preview first using the authorized deployment profile:

```powershell
python scripts/migrate-payee-roles.py --workspace customer-e3a574d2f38e4f1ba41e564591197d11 --correct-at-infra
```

Add `--apply` to commit. The script validates the active organization, discovers
its tables from CloudFormation, backs up the workspace to ignored local storage,
and commits atomically using the ledger revision. More than 99 changed records
are refused. The saved state is read back and verified, with a receipt under
`.local/payee-role-migration/`. No amounts, payment dates, bank references, or
repayment allocations change. Re-running an applied migration is a no-op.

The A&T INFRA correction flag also sets GD Saw Mill to Wood Supplier and
Sri Karthikeya to Steel Supplier / Rental, reclassifying their active expenses
from the confirmed former categories. It does not rename payees or create
unidentified employees. Existing staff salary months remain unknown unless a
confirmed mapping is passed with `--salary-month TRANSACTION_ID=YYYY-MM`.

Backups retain the entire pre-migration workspace and original ledger revision.
For rollback, compare the backup with current records and commit a reviewed
inverse change against the current revision; never overwrite newer payments
with a full old snapshot.

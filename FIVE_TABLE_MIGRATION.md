# Five-table correction — prepared locally, not deployed

The agreed storage model is five **physical DynamoDB tables**:

| Type | SAM resource | Stack output |
| --- | --- | --- |
| accounts | LedgerTable | AccountsTableName |
| vendors | VendorsTable | VendorsTableName |
| categories | CategoriesTable | CategoriesTableName |
| transactions | TransactionsTable | TransactionsTableName |
| repayment_allocations | RepaymentAllocationsTable | RepaymentAllocationsTableName |

The original physical table is retained as the accounts table to preserve existing data. The `LedgerTable` logical ID cannot be renamed casually because it is already deployed. Its `META` item still holds the workspace revision. Four additional on-demand, encrypted, recoverable tables are added. Existing `DynamoDbTableName` remains an alias for the accounts table for output compatibility.

`DYNAMODB_TABLES` contains all five names. Reads query all five tables and bracket the snapshot with a strongly consistent revision read. Changed records across all five tables and the revision guard are committed in a **single** DynamoDB transaction. The application still uses exactly three Lambdas and 12 routes. No frontend contract changes are required.

## Deployment status

These changes have not been applied to AWS. Automatic approval review could not complete because its usage limit was reached; no cloud inspection or deployment was executed for this correction. The previously deployed single-table application remains in place.

## Maintenance procedure

1. Pause application use and all writes, including administrator scripts, until verification completes. Keep a backup/export of the original table. Point-in-time recovery is already configured by the original SAM template.
2. In this backend repository run `python scripts/build-backend.py`, validate SAM, then deploy the updated template with the existing stack settings. This adds four tables and updates the three Lambdas without deleting the original table. During migration the new handlers deliberately return a conflict for misplaced legacy records rather than serving incomplete balances.
3. Preview the migration:

   ```powershell
   python scripts/migrate-five-tables.py
   ```

4. Inspect the counts, then apply:

   ```powershell
   python scripts/migrate-five-tables.py --apply
   python scripts/bootstrap-backend.py
   python scripts/verify-backend.py
   ```

5. Reopen application access only after migration and live verification succeed.

Migration is restricted to `WORKSPACE#clearbook`. It leaves accounts in the original table, creates each legacy record in its dedicated destination, removes that source record, and updates the revision atomically. Destination overwrites are prohibited. A concurrent application writer or destination conflict cancels the entire migration. A rerun after successful completion makes no further moves. Requests exceeding 49 moved records or the conservative size budget are rejected before any writes; such a ledger requires a separately planned staged maintenance migration. No tables are deleted.

## Table creation script

`python scripts/create-table.py --dry-run` checks all five stack outputs after deployment. `--table-name clearbook-ledger` is now a **prefix** and creates `clearbook-ledger-accounts`, `clearbook-ledger-vendors`, `clearbook-ledger-categories`, `clearbook-ledger-transactions`, and `clearbook-ledger-repayment_allocations`. Add `--initialize-categories` to initialize the category documents. Standalone tables do not automatically replace SAM-managed resources.

Local offline development continues to use five named document collections in the existing local JSON file. It does not create local DynamoDB tables. The old monolithic workspace outside this repository is an archived source snapshot; make further backend changes in this repository.

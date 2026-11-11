# Dedicated organization and user registry

Provider operations use two dedicated DynamoDB tables:

| Table | Records |
| --- | --- |
| `clearbook-organizations` | Organization names, IDs, enabled/deleted state and lifecycle timestamps |
| `clearbook-users` | Cognito subject IDs, email, organization membership and enabled/deleted state |

Both use `PK = WORKSPACE#__provider__`. Organization sort keys are `organizations#customer#<organization-id>`; user sort keys are `users#user#<cognito-subject>`. The registry revision lives in the organizations table. Registry reads and writes never query the financial accounts table. Customer ledger reads omit the registry tables after authorization. Cognito retains credentials and password management.

## Access lifecycle

Provider GET/POST/PATCH routes retain their existing URLs. DELETE `/api/provider/users/{id}` archives a user membership and revokes application access. DELETE `/api/provider/customers/{id}` archives the organization and its memberships atomically. Deleted records are excluded from provider lists and seat counts. They cannot be re-enabled with PATCH. A deleted user may be onboarded again using their existing Cognito account; the prior membership is retained in the replacement record's history.

Deletion archives access records (`deleted_at`, `deleted_by`, `enabled=false`), preserving financial records and Cognito identities. It does not erase ledgers or permanently remove Cognito accounts. Disable/enable updates include the actor and timestamp. Every customer API request checks active membership and organization state.

## Deployment and migration

Production verification completed on 2026-11-08: `clearbook-organizations` and `clearbook-users` are ACTIVE with point-in-time recovery enabled. All three deployed Lambda packages match the tested registry build and reference the dedicated tables. Provider organization and user list endpoints both returned HTTP 200. The source provider partition had zero items, and both registries had zero active records; no existing onboarding data required moving.

The following procedure remains the deployment/migration checklist for another environment or existing legacy registry data.

1. Provision `clearbook-organizations` and `clearbook-users` with string PK/SK keys, encryption and point-in-time recovery. `python scripts/create-table.py --table-name clearbook --dry-run` previews all eight physical tables; remove `--dry-run` to create missing tables and validate the existing tables. Preserve any custom table names.
2. Run `python scripts/migrate-provider-registry.py` for a read-only preview. It discovers the source accounts table from the stack. For custom new table names, pass `--organizations-table` and `--users-table` until the stack exports them.
3. Schedule a maintenance window covering migration and backend deployment. Pause provider changes and customer access: the older backend cannot authenticate memberships after they move, and the newer backend cannot authenticate them before they move.
4. Run the migration with `--apply`. It moves only provider organization/user documents from the provider partition of the accounts table, preserving every record and ID. A single DynamoDB transaction guards the source and destination revisions, writes each destination record conditionally, and removes its source copy. Any conflict aborts the entire move. Business partitions are untouched. More than 49 records requires a separately reviewed staged migration; the script refuses to split the operation silently.
5. Build with `python scripts/build-backend.py`, validate the SAM template, and deploy. Preserve existing parameters and add `OrganizationsTable` and `UsersTable` if using custom names. `DYNAMODB_TABLES` must include both new mappings. The template grants registry read permissions to the accounting handlers and registry write permissions to the reference handler, and adds DELETE routes/CORS support.
6. Verify signed-in provider lists, customer access, disable/enable and deletion using a test organization. Publish the frontend with Delete confirmation dialogs, then reopen access.

Do not roll back only the Lambda code after moving memberships: the older code reads the accounts table. Rollback requires a reviewed reverse migration during maintenance. The source migration does not delete any financial records. Keep the new tables and recovery enabled.

Local JSON development automatically moves legacy provider documents to separate `organizations` and `users` collections when loading the provider workspace. AWS migrations are always explicit. Tests use disposable local files or stubbed DynamoDB clients and never change live registry records.

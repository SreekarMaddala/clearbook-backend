# Provider and customer access pending deployment

The source separates software-provider management from organization ledgers. The provider onboards organizations and up to two users for each organization; it cannot read the ledgers. Deploy the backend and frontend together; see [deployment instructions](DEPLOYMENT.md). Existing shared ledger data is preserved and is not automatically assigned to a customer. The hosted changes have not been deployed.

`SUPER_ADMIN` means **software provider**. Providers onboard organizations and their users. Provider accounts cannot access business accounts, vendors, transactions, categories or dashboard totals. Customers see the business app for their own workspace.

## Provider dashboard

Sign in with the existing provider account. **Organizations / Onboard organization** creates an empty organization workspace and its 11 categories. **Users / Add user** links an email to that organization. Each organization has at most two customer users. New Cognito users receive a temporary password and must change it at first sign-in. Share it privately; Cognito invitations are suppressed. Existing Cognito users keep their password. A user already assigned to a different organization cannot be moved through this API, and provider accounts cannot be assigned as customer users.

Disable controls update server-side membership. Disabling a user or customer blocks access on their next API request. Accounts without active membership, including users created by previous setup scripts, cannot load ledger data.

## Storage and authorization

The existing five DynamoDB tables and three Lambdas are retained. Each organization uses a server-generated `WORKSPACE#customer-<uuid>` partition. Provider organization and membership records use `WORKSPACE#__provider__`. Both are account documents in different partitions; ledger routes cannot return provider records.

`GET /api/session` resolves the signed-in user. Membership is keyed by Cognito subject, not a client-supplied email. Every hosted request checks membership and customer status before selecting a workspace. Body fields, query parameters and URL fragments cannot select a tenant. API Gateway continues to validate the Cognito token and scope.

Existing `WORKSPACE#clearbook` records are preserved and are **not automatically assigned** to a new customer. New organizations start with empty ledgers. Assigning existing business data requires a separate, explicitly reviewed migration. Loopback development remains an unauthenticated demo ledger.

## Provider API additions

All paths start with `/api`. The 12 ledger endpoints listed below require an active organization membership. Seven endpoints are served by the reference Lambda:

| Method | Path | Access |
| --- | --- | --- |
| GET | `/session` | Signed-in provider or active customer |
| GET, POST | `/provider/customers` | Provider only |
| PATCH | `/provider/customers/{id}` | Provider only; `{ "enabled": true/false }` |
| GET, POST | `/provider/users` | Provider only |
| PATCH | `/provider/users/{id}` | Provider only; `{ "enabled": true/false }` |

Customer creation accepts `{ "name": "Customer name" }`. User creation accepts `email`, `customer_id` and `temporary_password`. Passwords are sent to Cognito in memory; the registry never stores them. Temporary passwords must contain at least 12 characters with uppercase, lowercase, a number and a symbol. The reference Lambda receives a pool-scoped `USER_POOL_ID` and only the Cognito permissions needed to create users and check group membership. No AWS administrator credentials reach the browser.

## Deployment and verification

Run the backend tests and Lambda packaging script, validate the SAM template, deploy the backend with the existing stack configuration, then deploy the frontend using its Amplify pipeline. The template adds seven protected routes and limited Cognito permissions. Confirm the five external DynamoDB tables and migration state before deployment. After both deployments, sign in as the provider, create a customer and user, then verify that the customer can access only its own ledger and that disabling membership blocks later requests.

Local backend tests cover role separation, direct API denial, workspace isolation, concurrent requests, membership revocation, validation and Cognito calls. Frontend tests exercise the actual provider/customer UI handlers. Local tests cannot verify live Cognito sign-in or deployed IAM permissions.

# Five-table correction pending deployment

The backend now defines five separate physical DynamoDB tables: accounts, vendors, categories, transactions, and repayment_allocations. This correction is tested locally but has not been applied to AWS. See [migration and deployment status](../FIVE_TABLE_MIGRATION.md). The historical deployment details below describe the prior single-table release.

# Clearbook DynamoDB backend

Production storage is **Amazon DynamoDB in Hyderabad (`ap-south-2`)**. There is no SQL, RDS, database password, VPC or NAT gateway in this stack.

## Architecture

- Five on-demand DynamoDB tables, encrypted, with point-in-time recovery and retention on stack deletion.
- Five document types: accounts, vendors, categories, transactions, repayment allocations.
- Exactly three Lambdas: reference and provider access (14 routes), transactions (4 routes), dashboard (1 route).
- Cognito User Pool with public signup disabled. API Gateway validates access tokens and the `clearbook/ledger` scope. Handlers enforce software-provider or customer membership for every request.
- The browser frontend uses Cognito sign-in and supports separate provider and customer views; the source update is pending deployment.

Customer ledger items use `PK = WORKSPACE#<customer-id>` and `SK = <entity>#<record-id>`. Provider registry items use the reserved `WORKSPACE#__provider__` partition. A `META` item stores the ledger revision. Items are separate DynamoDB records, not one giant ledger document. Organization workspaces can each have up to two customer users. It currently loads the workspace with paginated strongly consistent Query operations; query/index-based projections should replace whole-workspace loads for a large ledger.

## Accounting and concurrency

Amounts are validated with Decimal and persisted as integer paise. Each transaction requires a location (trimmed text, up to 200 characters); vendors are optional for expenses. Paybacks reduce liability without creating another expense. External-to-business transfers also create creditor liability.

Repayments allocate FIFO to funding on or before the repayment date. Same-day funding is eligible first. Overpayments and changes that invalidate historical repayments are rejected. All changed records, allocation differences and the revision condition are submitted in one DynamoDB `TransactWriteItems` request. A concurrent writer causes a 409 rather than losing an update. SDK retries carry a transaction request token. The original HTTP transaction creation also requires an `Idempotency-Key` header, preventing duplicate entries after retry.

Updates require the latest transaction `version`. Previous values, actor and time are retained in audit history. Reversals keep the original record and require a reason. The app rejects a change affecting more than 99 records plus the revision item, an oversized transaction, or an oversized audit record, before issuing writes. It never splits financial changes into non-atomic batches. Large repayments may need to be split into smaller entries. These bounds follow [DynamoDB transaction limits](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html).

## Ledger API (12 endpoints)

All routes start with `/api`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/accounts` | List accounts |
| POST | `/accounts` | Add account |
| PATCH | `/accounts/{id}` | Update account |
| GET | `/vendors` | List vendors |
| POST | `/vendors` | Add vendor |
| PATCH | `/vendors/{id}` | Update vendor |
| GET | `/categories` | List 11 fixed categories |
| GET | `/transactions` | List/filter/paginate with details and allocations |
| POST | `/transactions` | Create expense/payback/transfer |
| PATCH | `/transactions/{id}` | Update with version |
| POST | `/transactions/{id}/reverse` | Reverse with reason and version |
| GET | `/dashboard` | Totals and creditor balances |

Listing supports `page`, `limit` (1-200), `q`, `payer`, `vendor`, `category`, `location`, `type`, `status`, `from`, `to`, `include_reversed=true`. Reversed records are excluded by default. Invalid input returns a JSON `error`. Conflicts return 409. Categories are fixed to the user's 11 categories. Accounts used in transactions cannot change ownership type.

Create example (IDs must exist):

```json
{
  "date": "2026-11-04",
  "amount": "32500.25",
  "type": "EXPENSE",
  "payer": "business",
  "vendor": "steel",
  "category": "Other Expense",
  "mode": "NEFT",
  "location": "Hyderabad - Block A",
  "notes": "Steel reinforcement delivery"
}
```

PATCH accepts changed fields plus `version`. Reverse body: `{"reason":"Duplicate entry","version":1}`. Reference records are editable but not hard-deleted. Receipt attachments currently retain filenames only; actual file uploads/downloads are not implemented.

## Local frontend and offline backend

Run `python -m backend.server --seed-demo` for the API at http://127.0.0.1:8000. Run the sibling frontend repository separately with `npm run dev`. Local development uses a file-backed document adapter at `.local/clearbook.json` so it works without AWS access. It uses process/file locks, fsync and atomic file replacement; it is not advertised as DynamoDB Local. Existing SQLite/browser data is left untouched and is not automatically imported. Set `LOCAL_DATA_PATH` to choose a different local document file. The local server is loopback-only and unauthenticated.

`python -m backend.server --seed-demo` seeds sample data only into a fresh local document file. `python -m backend.server` initializes just the 11 categories; it never clears an existing store. Demo seeding is prohibited when `DYNAMODB_TABLE` is configured. Lambda requires that environment variable and cannot fall back to local files.

The frontend loads once on startup, after successful saves, or when **Refresh** is clicked. There is no endpoint polling. Filtering/searching/charts/export use the last loaded snapshot. The current chart displays June-November 2026 sample periods; summary totals cover all dates.

## Checks

Run `python -m unittest discover -s backend/tests -v`. The cross-repository frontend integration test belongs to `clearbook-frontend`. Backend tests cover accounting, concurrency, auditing, auth context and validation. Boto3 Stubber tests validate DynamoDB encoding, paginated consistent reads, revision conditions, conflicts and transaction limits. The integration test runs the frontend handlers against a real local HTTP server with DOM stubs; it is not a visual browser test.

See [DEPLOYMENT.md](DEPLOYMENT.md) for deployment, provider password setup and stack verification.

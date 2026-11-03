# Clearbook DynamoDB backend

Production storage is **Amazon DynamoDB in Hyderabad (`ap-south-2`)**. There is no SQL, RDS, database password, VPC or NAT gateway in this stack.

## Architecture

- One on-demand DynamoDB table, encrypted, with point-in-time recovery and retention on stack deletion.
- Five document types: accounts, vendors, categories, transactions, repayment allocations.
- Exactly three Lambdas: reference data (7 routes), transactions (4 routes), dashboard (1 route).
- Cognito User Pool with public signup disabled. API Gateway validates access tokens and the `clearbook/ledger` scope. The handlers additionally reject missing authentication, ID tokens and missing scopes.
- The frontend stays local for now; Vercel deployment and the browser login flow are deferred.

Items use `PK = WORKSPACE#clearbook` and `SK = <entity>#<record-id>`. A `META` item stores the ledger revision. Items are separate DynamoDB records, not one giant ledger document. The workspace snapshot design is intended for this small, two-user app. It currently loads the workspace with paginated strongly consistent Query operations; query/index-based projections should replace whole-workspace loads for a large ledger.

## Accounting and concurrency

Amounts are validated with Decimal and persisted as integer paise. Each transaction requires a location (trimmed text, up to 200 characters); vendors are optional for expenses. Paybacks reduce liability without creating another expense. External-to-business transfers also create creditor liability.

Repayments allocate FIFO to funding on or before the repayment date. Same-day funding is eligible first. Overpayments and changes that invalidate historical repayments are rejected. All changed records, allocation differences and the revision condition are submitted in one DynamoDB `TransactWriteItems` request. A concurrent writer causes a 409 rather than losing an update. SDK retries carry a transaction request token. The original HTTP transaction creation also requires an `Idempotency-Key` header, preventing duplicate entries after retry.

Updates require the latest transaction `version`. Previous values, actor and time are retained in audit history. Reversals keep the original record and require a reason. The app rejects a change affecting more than 99 records plus the revision item, an oversized transaction, or an oversized audit record, before issuing writes. It never splits financial changes into non-atomic batches. Large repayments may need to be split into smaller entries. These bounds follow [DynamoDB transaction limits](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html).

## API ? 12 endpoints

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

See [DEPLOYMENT.md](DEPLOYMENT.md) for Hyderabad deployment and provisioning the two users.

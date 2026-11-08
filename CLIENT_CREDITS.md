# Clients and incoming credits

The frontend now offers Expense, Payback and Credit when adding transactions. Existing transfers remain labeled Transfer and can still be viewed, edited and reversed. They are not converted to client receipts. Clients belong to each customer workspace; they do not create sign-in accounts or provider organizations.

## Deployment order

1. Create the dedicated clients table before deploying the new backend. For the current default `clearbook-*` table names, run `python scripts/create-table.py --table-name clearbook --dry-run` to review, then the same command without `--dry-run`. This validates the existing tables and creates missing tables with encryption and recovery enabled. If existing tables use another prefix, use the matching prefix or provision the clients table separately with string PK/SK keys. Do not change existing table parameters.
2. Build with `python scripts/build-backend.py` and validate with `sam validate --lint --template-file template.yaml --region ap-south-2`.
3. Deploy the backend with its existing parameters, adding `ClientsTable` if its name differs from `clearbook-clients`. The template adds clients to the environment mapping, IAM permissions, reference routes and outputs. All six entity mappings must be present in `DYNAMODB_TABLES`. This adds no new Lambda function.
4. If the older single-table migration is still pending, follow the existing maintenance migration procedure with the updated `scripts/migrate-five-tables.py`. An already migrated five-table deployment only needs the empty clients table and configuration update; no existing business records need moving.
5. Publish the new frontend after the backend supports `/api/clients` and `CREDIT`.

The new frontend requires the new clients API. Deploying it first would cause the initial data load to fail. Keep the new table when rolling back; do not rewrite credits as transfers. An older frontend does not understand credits, so rolling back only the frontend after recording credits is not supported.

## Local verification

Run `python -m unittest discover -s backend/tests` in this repository. In the frontend repository run `npm run check`, `npm test`, `npm run test:integration`, and `npm run build`. Integration tests use disposable local data and exercise clients, credit creation/edit/reversal, receipt totals and unchanged expense/creditor totals.

No AWS deployment or table creation is performed by the tests or this source change.

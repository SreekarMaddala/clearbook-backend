# Dedicated provider registry deployed

Organization onboarding, memberships, enable/disable and access deletion now use dedicated `clearbook-organizations` and `clearbook-users` tables. The dedicated tables were created and live read-only verification passed on 2026-11-08. Both tables have point-in-time recovery enabled. The old provider partition contained no onboarding records, so no migration writes were needed. See [PROVIDER_REGISTRY.md](PROVIDER_REGISTRY.md).

# Clients and incoming credits pending deployment

The source now adds a Clients directory and incoming `CREDIT` transactions. Storage requires six entity mappings, including a dedicated clients table. Tested local changes have not been deployed to AWS. See [client credit deployment](CLIENT_CREDITS.md) before deploying the backend or frontend.

# Provider/customer separation pending deployment

The source adds separate software-provider and organization-user access. The provider dashboard onboards organizations and assigns up to two users to each organization; each organization receives an isolated ledger. Deployment and security details are in [backend/README.md](backend/README.md#provider-and-customer-access). Existing shared ledger data is preserved and is not automatically assigned to an organization.

# Five-table correction pending deployment

The backend now defines five separate physical DynamoDB tables: accounts, vendors, categories, transactions, and repayment_allocations. This correction is tested locally but has not been applied to AWS. See [migration and deployment status](FIVE_TABLE_MIGRATION.md). The historical deployment details below describe the prior single-table release.

# Clearbook backend

Independent backend repository: DynamoDB, Cognito, API Gateway and exactly three Python Lambda functions in Hyderabad (`ap-south-2`). Frontend source is maintained separately in `clearbook-frontend`.

## Run locally

```powershell
python -m pip install -r requirements-dev.txt
python -m backend.server --seed-demo
```

API: http://127.0.0.1:8000/api. Start the frontend from its own checkout with `npm run dev`; its development proxy forwards `/api` to this server. The backend does not depend on frontend files or Node.js. Offline storage uses `.local/clearbook.json`; Lambda uses DynamoDB. Omit `--seed-demo` to initialize an empty local store with the 11 categories.

## Tests and deployment

```powershell
python -m unittest discover -s backend/tests -v
python scripts/build-backend.py
sam validate --lint --template-file template.yaml --region ap-south-2
sam deploy
```

The SAM defaults point to the existing `clearbook-hyderabad` stack. Build before deploying. AWS credentials are read from your environment/profile; no credentials, local data or generated archives are tracked.

Standalone table script: `python scripts/create-table.py --dry-run`. Super-admin password setup: `python scripts/bootstrap-backend.py --set-admin-password` (masked terminal prompt).

- [Data model and API contract](backend/README.md)
- [Deployed resources and deployment instructions](backend/DEPLOYMENT.md)

The live API is JWT-protected. Local development is loopback-only and unauthenticated. This repository split does not change deployed AWS resources.

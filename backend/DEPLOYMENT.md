# Provider/customer update pending deployment

See [provider access](README.md#provider-and-customer-access) before deploying this source. `SUPER_ADMIN` is the software provider and cannot access organization ledgers. Onboard organizations and up to two users per organization through the provider UI after both deployments. The old `--add-user` command has been removed. Existing users require organization membership; existing ledger records are preserved. Deployment details below include historical behavior.

# Current deployment configuration: use existing tables

The current SAM template uses the five manually created `clearbook-accounts`, `clearbook-vendors`, `clearbook-categories`, `clearbook-transactions`, and `clearbook-repayment_allocations` tables. It does not create or import DynamoDB table resources. The previous update failed because the original SAM-managed LedgerTable was manually deleted. That reference is now removed. The Cognito resources remain unchanged.

From the backend repository:

```powershell
python scripts/create-table.py --table-name clearbook --initialize-categories
python scripts/build-backend.py
sam deploy --no-confirm-changeset
python scripts/bootstrap-backend.py
python scripts/verify-backend.py
```

Run each command only after the previous command succeeds. The five table-name parameters default to the exact names above. Inspect the change set before deployment if you have customized those names. No legacy migration is required if you deliberately deleted the original data and are starting fresh. Do not delete any retained rollback tables until their contents have been checked. This change has only been checked locally; live redeployment is still required. Historical migration/deployment notes below are superseded by this section.

---

# Five-table correction pending deployment

The backend now defines five separate physical DynamoDB tables: accounts, vendors, categories, transactions, and repayment_allocations. This correction is tested locally but has not been applied to AWS. See [migration and deployment status](../FIVE_TABLE_MIGRATION.md). The historical deployment details below describe the prior single-table release.

# Hyderabad backend deployment - DynamoDB

Deployment verified on 4 November 2026. Stack status: `UPDATE_COMPLETE`.

- API: `https://6fevwj9dy0.execute-api.ap-south-2.amazonaws.com/api` (JWT required)
- DynamoDB table: `clearbook-hyderabad-LedgerTable-JT03VYIDP1R9`
- Cognito User Pool: `ap-south-2_vy4GCYTNM`
- App client: `qf45j6u4nn5keh80oo07kbr91`
- Super admin: `sreekarmaddala9999@gmail.com`, enabled in `SUPER_ADMIN`; private password setup required.
- 11 categories initialized. Business data starts empty.

Live checks verified all three handlers, all 12 scoped JWT routes, unauthenticated rejection, localhost CORS, and real DynamoDB atomic accounting/idempotency/concurrency. Synthetic write checks used an isolated random workspace, which was removed afterwards. Run `python scripts/verify-backend.py` with the deployment IAM profile to repeat these checks.

Backend stack: **clearbook-hyderabad**. Region: **ap-south-2 (Hyderabad)**. `samconfig.toml` pins the region.

The SAM template provisions five DynamoDB on-demand tables, exactly three Lambda functions, API Gateway, and Cognito. No PostgreSQL/RDS, secrets, network subnets or security groups are required. The frontend remains local; Vercel hosting and the browser login/PKCE flow are deferred.

## Deploy

For standalone table setup, use `python scripts/create-table.py --table-name clearbook-ledger`. It creates the string PK/SK table with on-demand capacity, encryption and point-in-time recovery. Re-running verifies the schema without changing an existing table. The default `python scripts/create-table.py` discovers and validates the existing SAM table rather than creating a duplicate. Add `--dry-run` for read-only checks, `--profile PROFILE` for a named AWS profile, or `--initialize-categories` to add the 11 category documents. A standalone table is not automatically attached to the deployed Lambda stack; SAM remains the source of truth for stack-managed resources.

First verify the AWS identity using the intended profile:

```powershell
aws sts get-caller-identity --region ap-south-2
sam validate --lint --template-file template.yaml --region ap-south-2
python scripts/build-backend.py
sam deploy --template-file .local/deploy/template.yaml --stack-name clearbook-hyderabad --region ap-south-2 --resolve-s3 --capabilities CAPABILITY_IAM --no-confirm-changeset --parameter-overrides SuperAdminEmail=YOUR_EMAIL
```

The packaging helper creates one shared zip for the three handlers. They use only Python's standard library and boto3 supplied by the Lambda runtime, so Docker and platform-specific dependency builds are unnecessary. It excludes seed data, tests, local storage and credentials.

Default CORS origins: http://localhost:5173 and http://127.0.0.1:5173. Cognito callbacks/logout URLs use the matching root URLs with trailing slash. Public signup is disabled. The public browser OAuth client has no client secret and supports authorization-code flow. The `clearbook/ledger` scope is required by every API route. Use authorization code with PKCE when implementing browser login.

For the eventual Vercel deployment, set SAM parameters `AllowedOrigin` and `CallbackUrl` to the exact Vercel origin and callback. No wildcard origins are used. The app client has 15-minute access tokens and seven-day refresh tokens. API Gateway validates JWTs locally, so previously issued access tokens can remain usable until expiry after logout/revocation.

## Initialize the backend and configure the provider account

```powershell
python -m pip install -r scripts/requirements.txt
python scripts/bootstrap-backend.py
```

This invokes an IAM-only administrative operation on the existing reference Lambda to initialize the 11 category documents. It creates no fourth Lambda or public endpoint. It does not insert business/sample data into the AWS table. Outputs are public identifiers: API base URL, table name, User Pool ID, client ID, Cognito domain and Lambda names.

The deployed initial account is `sreekarmaddala9999@gmail.com`, attached to the `SUPER_ADMIN` Cognito group. No password is embedded or printed, and no invitation email is sent. Set your password privately in an interactive terminal:

```powershell
python scripts/bootstrap-backend.py --set-admin-password
```

Organization users are now managed in the provider UI; see [provider access](README.md#provider-and-customer-access).

Use `--profile PROFILE` for a named AWS profile. The IAM identity needs permission to describe the stack and invoke the reference function. Set up the provider password with the administrative Cognito access described above. Customer users are created in the provider UI after deployment; browser users receive no AWS permissions.

## Local frontend

Run `python -m backend.server --seed-demo` for the local API at http://127.0.0.1:8000; start the frontend from its separate repository. This keeps offline development available through the local document adapter. The frontend repository configures its public API base and Cognito login flow. Deploy it through the existing Amplify pipeline with the matching API origin and Cognito callback. Do not store AWS credentials or customer data in frontend source.

See [README.md](README.md) for the data model, transaction bounds, API contract and verification. The retained DynamoDB table and Cognito pool are preserved when the stack is deleted; assess stored data before any separate removal.

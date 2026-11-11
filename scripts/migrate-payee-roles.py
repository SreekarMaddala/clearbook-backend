"""Preview/apply payee roles and confirmed A&T INFRA category corrections.

No tables are recreated. Writes use the ledger revision condition and retain a
workspace backup. Historical salary months are left unknown unless supplied.
"""
import argparse
import copy
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.database import DynamoStore, PLATFORM_WORKSPACE, REGISTRY_ENTITIES
from backend.service import PAYEE_ROLES, SALARY_CATEGORY, payee_roles

AT_INFRA = 'customer-e3a574d2f38e4f1ba41e564591197d11'


def migrate_state(before, stamp, corrections=False, salary_months=None):
    after = copy.deepcopy(before)
    months = salary_months or {}
    for month in months.values():
        if not isinstance(month, str) or date.fromisoformat(month + '-01').strftime('%Y-%m') != month:
            raise ValueError('Salary months must use YYYY-MM.')
    if set(months) - set(before['transactions']):
        raise ValueError('Salary month refers to an unknown transaction.')
    targets = {}
    for key, vendor in after['vendors'].items():
        roles = payee_roles(vendor)
        if not isinstance(roles, list) or not roles or any(r not in PAYEE_ROLES for r in roles):
            raise ValueError('Invalid existing payee roles; review before migration.')
        vendor['roles'] = [r for r in PAYEE_ROLES if r in roles]
        target = None
        old_category = vendor.get('category', '')
        if vendor['roles'] == ['STAFF'] and old_category.casefold() in ('staff', 'staff salaries'):
            target = SALARY_CATEGORY
        if corrections and vendor['name'].strip().casefold() == 'gd saw mill':
            if old_category not in ('Other Expense', 'Wood/Runner supplier', 'Wood Supplier'):
                raise ValueError('Unexpected GD Saw Mill category; review before migration.')
            target = 'Wood Supplier'
            vendor.update(roles=['SUPPLIER'], role=target)
        if corrections and vendor['name'].strip().casefold() == 'sri karthikeya':
            if old_category not in ('Shuttering Sub Contractor', 'Steel Supplier / Rental'):
                raise ValueError('Unexpected Sri Karthikeya category; review before migration.')
            target = 'Steel Supplier / Rental'
            vendor.update(roles=['SUPPLIER'], role=target)
        if target:
            vendor['category'] = target
            targets[key] = (old_category, target)
        if vendor != before['vendors'][key]:
            vendor['updated_at'] = stamp
    categories = {SALARY_CATEGORY} | {target for _, target in targets.values()}
    for category in sorted(categories):
        if category not in after['categories']:
            after['categories'][category] = {
                'name': category,
                'sort_order': max((c.get('sort_order', 0) for c in after['categories'].values()), default=-1) + 1,
            }
    changed_transactions = []
    for key, transaction in after['transactions'].items():
        if key in months and (transaction.get('type') != 'EXPENSE' or transaction.get('reversed_at')):
            raise ValueError('Salary month must refer to an active salary expense.')
        if transaction.get('type') != 'EXPENSE' or transaction.get('reversed_at'):
            continue
        updates = {}
        target = targets.get(transaction.get('vendor'))
        if target:
            accepted = {target[0]}
            if corrections and target[1] == 'Wood Supplier':
                accepted.update(('Other Expense', 'Wood/Runner supplier'))
            if transaction.get('category') in accepted and transaction.get('category') != target[1]:
                updates['category'] = target[1]
        if key in months:
            if updates.get('category', transaction.get('category')) != SALARY_CATEGORY:
                raise ValueError('Salary month may only be assigned to a salary expense.')
            if transaction.get('salary_month') != months[key]:
                updates['salary_month'] = months[key]
        if not updates:
            continue
        history = json.loads(transaction['history'])
        history.append({'action': 'update', 'actor': 'user-authorized-payee-role-migration',
                        'at': stamp, 'before': {k: v for k, v in transaction.items() if k != 'history'}})
        transaction.update(updates, version=transaction['version'] + 1,
                           updated_at=stamp, history=json.dumps(history))
        changed_transactions.append(key)
    for entity in ('accounts', 'clients', 'locations', 'repayment_allocations'):
        if before[entity] != after[entity]:
            raise ValueError('Unrelated ledger records changed.')
    financial_fields = ('amount_paise', 'payer', 'recipient', 'date', 'type', 'vendor',
                        'mode', 'location', 'notes', 'reversed_at', 'source_import', 'request_hash')
    for key, row in before['transactions'].items():
        if any(row.get(f) != after['transactions'][key].get(f) for f in financial_fields):
            raise ValueError('A financial field changed.')
    return after, changed_transactions


def main():
    import boto3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--stack', default='clearbook-hyderabad')
    parser.add_argument('--profile')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--correct-at-infra', action='store_true')
    parser.add_argument('--salary-month', action='append', default=[], metavar='TRANSACTION_ID=YYYY-MM')
    args = parser.parse_args()
    if args.correct_at_infra and args.workspace != AT_INFRA:
        raise ValueError('A&T INFRA corrections cannot target another workspace.')
    months = dict(item.split('=', 1) for item in args.salary_month)
    session = boto3.Session(profile_name=args.profile, region_name='ap-south-2')
    identity = session.client('sts').get_caller_identity()
    outputs = {o['OutputKey']: o['OutputValue'] for o in session.client('cloudformation').describe_stacks(
        StackName=args.stack)['Stacks'][0]['Outputs']}
    keys = {'accounts': 'AccountsTableName', 'vendors': 'VendorsTableName', 'categories': 'CategoriesTableName',
            'transactions': 'TransactionsTableName', 'repayment_allocations': 'RepaymentAllocationsTableName',
            'clients': 'ClientsTableName', 'organizations': 'OrganizationsTableName', 'users': 'UsersTableName'}
    tables = {entity: outputs[key] for entity, key in keys.items()}
    client = session.client('dynamodb')
    registry = DynamoStore(tables, client=client, entities=REGISTRY_ENTITIES)
    registry.pk = 'WORKSPACE#' + PLATFORM_WORKSPACE
    _, access = registry.read()
    organization = access['organizations'].get('customer#' + args.workspace)
    if not organization or not organization.get('enabled') or organization.get('deleted_at'):
        raise ValueError('Target organization is not active.')
    if args.correct_at_infra and organization['name'] != 'A&T INFRA':
        raise ValueError('Unexpected organization name.')
    store = DynamoStore(tables, client=client)
    store.pk = 'WORKSPACE#' + args.workspace
    revision, before = store.read()
    stamp = datetime.now(timezone.utc).isoformat()
    after, changed_transactions = migrate_state(before, stamp, args.correct_at_infra, months)
    changes = [{'entity': entity, 'id': key} for entity in after for key, record in after[entity].items()
               if before[entity].get(key) != record]
    print(json.dumps({'organization': organization['name'], 'account': identity['Account'],
                      'revision': revision, 'changes': changes,
                      'payees': [{'name': v['name'], 'roles': v['roles'], 'category': v['category']}
                                 for v in after['vendors'].values()]}, indent=2))
    if len(changes) > 99:
        raise ValueError('Migration exceeds one atomic write; split into reviewed batches.')
    if not args.apply:
        print('PREVIEW ONLY: no data changed.')
        return
    output = ROOT / '.local' / 'payee-role-migration'
    output.mkdir(parents=True, exist_ok=True)
    prefix = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    backup = output / (prefix + '-before.json')
    backup.write_text(json.dumps({'workspace': args.workspace, 'revision': revision, 'state': before}, indent=2), encoding='utf-8')
    store.commit(revision, before, after)
    saved_revision, saved = store.read()
    if saved != after:
        raise RuntimeError('Verification differs from the expected data. Inspect before retrying.')
    receipt = {'workspace': args.workspace, 'applied_at': stamp, 'revision_before': revision,
               'revision_after': saved_revision, 'changes': changes, 'backup': str(backup),
               'reclassified_transactions': changed_transactions,
               'unknown_salary_months': [t['id'] for t in saved['transactions'].values()
                                         if t.get('category') == SALARY_CATEGORY and not t.get('salary_month') and not t.get('reversed_at')]}
    (output / (prefix + '-receipt.json')).write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print('APPLIED AND VERIFIED. Receipt:', output / (prefix + '-receipt.json'))


if __name__ == '__main__':
    main()

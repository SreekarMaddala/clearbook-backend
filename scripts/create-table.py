"""Create or validate a Clearbook DynamoDB table in Hyderabad.

Default: discover the existing SAM-managed table, without creating a duplicate.
Explicit --table-name PREFIX: create five standalone tables if missing.
"""
import argparse
import copy
import re
import sys
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

REGION = 'ap-south-2'
KEYS = [{'AttributeName': 'PK', 'KeyType': 'HASH'}, {'AttributeName': 'SK', 'KeyType': 'RANGE'}]
ATTRIBUTES = [{'AttributeName': 'PK', 'AttributeType': 'S'}, {'AttributeName': 'SK', 'AttributeType': 'S'}]


def validate_schema(table):
    keys = {k['AttributeName']: k['KeyType'] for k in table.get('KeySchema', [])}
    types = {a['AttributeName']: a['AttributeType'] for a in table.get('AttributeDefinitions', [])}
    if keys != {'PK': 'HASH', 'SK': 'RANGE'} or any(types.get(k) != 'S' for k in ('PK', 'SK')):
        raise ValueError('Existing table has an incompatible key schema. Expected string PK (partition) and SK (sort). No changes made.')


def ensure_table(client, name, dry_run=False):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,255}', name):
        raise ValueError('Table name must be 3-255 letters, numbers, underscores, hyphens or periods.')
    try:
        table = client.describe_table(TableName=name)['Table']
    except client.exceptions.ResourceNotFoundException:
        table = None
    if table:
        validate_schema(table)
        print(f'Existing table verified: {name}. No table settings changed.')
        return False
    if dry_run:
        print(f'Would create {name} in {REGION}: string PK/SK, on-demand capacity, encryption, point-in-time recovery.')
        return False
    created = False
    try:
        client.create_table(TableName=name, KeySchema=KEYS, AttributeDefinitions=ATTRIBUTES,
                            BillingMode='PAY_PER_REQUEST', SSESpecification={'Enabled': True},
                            Tags=[{'Key': 'Application', 'Value': 'clearbook'}])
        created = True
    except client.exceptions.ResourceInUseException:
        # Another administrator/process may have created this name after our lookup.
        print('Table was created concurrently; waiting and checking its schema.')
    client.get_waiter('table_exists').wait(TableName=name, WaiterConfig={'Delay': 3, 'MaxAttempts': 100})
    validate_schema(client.describe_table(TableName=name)['Table'])
    if created:
        client.update_continuous_backups(TableName=name, PointInTimeRecoverySpecification={'PointInTimeRecoveryEnabled': True})
        print(f'Created {name}; point-in-time recovery enabled.')
    else:
        print(f'Existing table verified: {name}. No table settings changed.')
    return created


def initialize_categories(client, name, workspace):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from backend.database import CATEGORIES, DynamoStore
    store = DynamoStore(name, client=client)
    store.pk = 'WORKSPACE#' + workspace
    revision, before = store.read()
    after = copy.deepcopy(before)
    for order, category in enumerate(CATEGORIES):
        after['categories'].setdefault(category, {'name': category, 'sort_order': order})
    store.commit(revision, before, after)
    print(f'11 categories initialized for workspace {workspace}; existing records preserved.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--table-name', help='Prefix for five standalone entity tables to create or validate.')
    target.add_argument('--stack', default='clearbook-hyderabad', help='Existing SAM stack; default: clearbook-hyderabad.')
    parser.add_argument('--profile', help='AWS CLI profile to use.')
    parser.add_argument('--dry-run', action='store_true', help='Read-only: show creation/settings without writing.')
    parser.add_argument('--initialize-categories', action='store_true', help='Add the 11 categories atomically, preserving existing data.')
    parser.add_argument('--workspace', default='clearbook')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', args.workspace):
        parser.error('Workspace must be 1-80 letters, numbers, hyphens or underscores.')
    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    client = session.client('dynamodb')
    if args.table_name:
        names = {entity:args.table_name+'-'+entity for entity in ('accounts','vendors','categories','transactions','repayment_allocations')}
    else:
        stack = session.client('cloudformation').describe_stacks(StackName=args.stack)['Stacks'][0]
        outputs = {o['OutputKey']:o['OutputValue'] for o in stack.get('Outputs',[])}
        keys = {'accounts':'AccountsTableName','vendors':'VendorsTableName','categories':'CategoriesTableName',
                'transactions':'TransactionsTableName','repayment_allocations':'RepaymentAllocationsTableName'}
        if outputs.get('AwsRegion') != REGION or not all(key in outputs for key in keys.values()):
            raise ValueError('Deploy the five-table SAM template first, or supply --table-name PREFIX for standalone tables.')
        names = {entity:outputs[key] for entity,key in keys.items()}
        for name in names.values():
            client.describe_table(TableName=name)
    for entity,name in names.items():
        ensure_table(client,name,dry_run=args.dry_run)
        print(f'{entity}: {name}')
    if args.initialize_categories:
        if args.dry_run:
            print('Would initialize 11 categories; no records written.')
        else:
            initialize_categories(client,names,args.workspace)
    import json
    print(f'Region: {REGION}')
    print('DYNAMODB_TABLES='+json.dumps(names))



if __name__ == '__main__':
    try:
        main()
    except (BotoCoreError, ClientError, ValueError) as error:
        sys.exit(f'Table setup failed: {error}')

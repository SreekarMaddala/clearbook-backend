"""Move provider organizations/memberships from accounts into their dedicated tables.

Read-only unless --apply is supplied. One revision-guarded DynamoDB transaction
copies and removes only provider registry records. Business partitions are untouched.
"""
import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.database import DynamoStore, PLATFORM_WORKSPACE, REGISTRY_ENTITIES


def migration_actions(source, destination, source_revision, destination_revision, before, registry):
    actions = []
    for key, record in before['accounts'].items():
        entity = {'customer':'organizations','user':'users'}.get(record.get('kind'))
        if not entity:
            continue
        expected_prefix = 'customer#' if entity == 'organizations' else 'user#'
        if not key.startswith(expected_prefix):
            raise ValueError('Unexpected registry key; inspect the source before applying.')
        if key in registry[entity]:
            raise ValueError('Destination already has this record. Inspect both copies before proceeding.')
        target = {'PK':destination.pk,'SK':entity+'#'+key,'entity':entity,'record_id':key,'data':record}
        actions.append({'Put':{'TableName':destination.tables[entity], 'Item':destination.encode(target),
                               'ConditionExpression':'attribute_not_exists(PK)'}})
        actions.append({'Delete':{'TableName':source.table,
                                  'Key':source.encode({'PK':source.pk,'SK':'accounts#'+key})}})
    if not actions:
        return []
    if len(actions) > 98:
        raise ValueError('More than 49 records require moving; use a separately reviewed staged migration.')
    for store, revision in ((source,source_revision),(destination,destination_revision)):
        meta = {'TableName':store.table, 'Item':store.encode({'PK':store.pk,'SK':'META','revision':revision+1})}
        if revision:
            meta.update(ConditionExpression='#r = :expected',ExpressionAttributeNames={'#r':'revision'},
                        ExpressionAttributeValues={':expected':{'N':str(revision)}})
        else:
            meta['ConditionExpression']='attribute_not_exists(PK)'
        actions.append({'Put':meta})
    if len(json.dumps(actions).encode()) > 3500000:
        raise ValueError('Migration is too large for one atomic transaction.')
    return actions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stack',default='clearbook-hyderabad')
    parser.add_argument('--profile')
    parser.add_argument('--organizations-table',default='clearbook-organizations')
    parser.add_argument('--users-table',default='clearbook-users')
    parser.add_argument('--apply',action='store_true')
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile,region_name='ap-south-2')
    stack = session.client('cloudformation').describe_stacks(StackName=args.stack)['Stacks'][0]
    outputs = {r['OutputKey']:r['OutputValue'] for r in stack['Outputs']}
    client = session.client('dynamodb')
    source = DynamoStore(outputs['AccountsTableName'],client=client)
    source.pk = 'WORKSPACE#'+PLATFORM_WORKSPACE
    destination = DynamoStore({'organizations':outputs.get('OrganizationsTableName',args.organizations_table),
                              'users':outputs.get('UsersTableName',args.users_table)},
                             client=client,entities=REGISTRY_ENTITIES)
    destination.pk = source.pk
    source_revision,before = source.read()
    destination_revision,registry = destination.read()
    actions = migration_actions(source,destination,source_revision,destination_revision,before,registry)
    if not actions:
        print('No provider records remain in the accounts table; nothing to migrate.')
        return
    count = (len(actions)-2)//2
    print(f'{count} provider records will move atomically. Business ledger partitions are unchanged.')
    if not args.apply:
        print('Read-only preview. Pause provider onboarding/changes during migration and deployment, then use --apply.')
        return
    client.transact_write_items(TransactItems=actions,ClientRequestToken=str(uuid4()))
    _,after = destination.read()
    for key,record in before['accounts'].items():
        entity = {'customer':'organizations','user':'users'}.get(record.get('kind'))
        if entity and after[entity].get(key) != record:
            raise RuntimeError('Migration verification failed. Keep provider changes paused.')
    print(f'Migration verified: {count} provider records preserved in dedicated registry tables.')


if __name__ == '__main__':
    main()

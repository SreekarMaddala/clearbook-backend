"""Move legacy entity documents to their dedicated tables in one atomic operation.

Run after deploying the five-table template. Default is read-only; --apply writes.
Keep application writes paused until migration succeeds. No business record is
deleted: each source removal is atomic with its destination creation.
"""
import argparse
import sys
from pathlib import Path
from uuid import uuid4

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.database import DynamoStore

OUTPUTS = {'accounts':'AccountsTableName', 'vendors':'VendorsTableName',
           'categories':'CategoriesTableName', 'transactions':'TransactionsTableName',
           'repayment_allocations':'RepaymentAllocationsTableName'}


def migration_actions(store, revision, state, tables):
    actions = []
    for entity, records in state.items():
        if entity == 'accounts':
            continue
        for key, data in records.items():
            item_key = {'PK':store.pk, 'SK':entity + '#' + key}
            item = {**item_key, 'entity':entity, 'record_id':key, 'data':data}
            actions.append({'Put':{'TableName':tables[entity], 'Item':store.encode(item),
                                   'ConditionExpression':'attribute_not_exists(PK)'}})
            actions.append({'Delete':{'TableName':store.table, 'Key':store.encode(item_key)}})
    if not actions:
        return []
    if len(actions) > 98:
        raise ValueError('More than 49 legacy records require moving. Stop here and prepare a staged maintenance migration; no records were changed.')
    actions.append({'Put':{'TableName':store.table,
                           'Item':store.encode({'PK':store.pk, 'SK':'META', 'revision':revision + 1}),
                           'ConditionExpression':'#r = :expected',
                           'ExpressionAttributeNames':{'#r':'revision'},
                           'ExpressionAttributeValues':{':expected':{'N':str(revision)}}}})
    import json
    if len(json.dumps(actions).encode()) > 3500000:
        raise ValueError('Migration exceeds the atomic request size budget; no records were changed.')
    return actions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stack', default='clearbook-hyderabad')
    parser.add_argument('--profile')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name='ap-south-2')
    outputs = {o['OutputKey']:o['OutputValue'] for o in session.client('cloudformation').describe_stacks(StackName=args.stack)['Stacks'][0]['Outputs']}
    tables = {entity:outputs[key] for entity,key in OUTPUTS.items()}
    client = session.client('dynamodb')
    legacy = DynamoStore(tables['accounts'], client=client)
    revision, state = legacy.read()
    actions = migration_actions(legacy, revision, state, tables)
    print('Legacy records by type:', {entity:len(rows) for entity,rows in state.items()})
    if not actions:
        print('No legacy records to move. Existing dedicated tables are unchanged.')
        return
    print(f'{(len(actions)-1)//2} records will move atomically; accounts and workspace revision remain in the original table.')
    if not args.apply:
        print('Read-only preview. Run with --apply during the maintenance window to migrate.')
        return
    client.transact_write_items(TransactItems=actions, ClientRequestToken=str(uuid4()))
    _, migrated = DynamoStore(tables, client=client).read()
    for entity, records in state.items():
        for key, value in records.items():
            if migrated[entity].get(key) != value:
                raise RuntimeError('Post-migration verification failed; inspect the tables before reopening writes.')
    print('Migration verified: all original records preserved in their respective tables.')


if __name__ == '__main__':
    main()

import importlib.util
from pathlib import Path
import unittest

import boto3

from backend.database import DynamoStore, empty_state

spec = importlib.util.spec_from_file_location('migration', Path(__file__).resolve().parents[2] / 'scripts' / 'migrate-five-tables.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class MigrationTest(unittest.TestCase):
    def setUp(self):
        client = boto3.client('dynamodb', region_name='ap-south-2', aws_access_key_id='test', aws_secret_access_key='test')
        self.store = DynamoStore('legacy', client=client)
        self.tables = {entity:'new-'+entity for entity in empty_state()}
        self.tables['accounts'] = 'legacy'

    def test_moves_preserve_data_and_guard_revision(self):
        state = empty_state()
        state['accounts']['a'] = {'name':'Business'}
        state['vendors']['v'] = {'name':'Shree Balaji Steels'}
        actions = migration.migration_actions(self.store, 5, state, self.tables)
        self.assertEqual(len(actions),3)
        self.assertEqual(actions[0]['Put']['TableName'],'new-vendors')
        self.assertEqual(actions[0]['Put']['ConditionExpression'],'attribute_not_exists(PK)')
        self.assertEqual(self.store.decode(actions[0]['Put']['Item'])['data'],state['vendors']['v'])
        self.assertEqual(actions[1]['Delete']['TableName'],'legacy')
        self.assertEqual(actions[2]['Put']['ExpressionAttributeValues'],{':expected':{'N':'5'}})

    def test_noop_when_only_accounts_remain(self):
        state=empty_state()
        state['accounts']['a']={'name':'Business'}
        self.assertEqual(migration.migration_actions(self.store,2,state,self.tables),[])

    def test_large_migration_fails_before_writing(self):
        state=empty_state()
        state['vendors']={str(i):{'name':str(i)} for i in range(50)}
        with self.assertRaisesRegex(ValueError,'49'):
            migration.migration_actions(self.store,2,state,self.tables)

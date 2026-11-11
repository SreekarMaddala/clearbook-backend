import importlib.util
from pathlib import Path
import unittest

import boto3

from backend.database import DynamoStore, REGISTRY_ENTITIES, empty_state

spec = importlib.util.spec_from_file_location('registry_migration',Path(__file__).resolve().parents[2]/'scripts'/'migrate-provider-registry.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class RegistryMigrationTest(unittest.TestCase):
    def setUp(self):
        client = boto3.client('dynamodb',region_name='ap-south-2',aws_access_key_id='test',aws_secret_access_key='test')
        self.source = DynamoStore('accounts',client=client)
        self.destination = DynamoStore({'organizations':'organizations','users':'users'},client=client,entities=REGISTRY_ENTITIES)
        self.source.pk = self.destination.pk = 'WORKSPACE#__provider__'
        self.before = empty_state()
        self.before['accounts']['customer#one'] = {'id':'one','kind':'customer','name':'One'}
        self.before['accounts']['user#alice'] = {'id':'alice','kind':'user','customer_id':'one'}
        self.before['accounts']['business'] = {'id':'business','type':'INTERNAL'}

    def actions(self,registry=None):
        return migration.migration_actions(self.source,self.destination,3,0,self.before,registry or empty_state(REGISTRY_ENTITIES))

    def test_preserves_records_and_guards_both_revisions(self):
        actions = self.actions()
        self.assertEqual(len(actions),6)
        copies = [a['Put'] for a in actions[:-2] if 'Put' in a]
        self.assertEqual({c['TableName'] for c in copies},{'organizations','users'})
        self.assertEqual(self.destination.decode(copies[0]['Item'])['data'],self.before['accounts']['customer#one'])
        removals = [self.source.decode(a['Delete']['Key'])['SK'] for a in actions if 'Delete' in a]
        self.assertEqual(removals,['accounts#customer#one','accounts#user#alice'])
        self.assertEqual(actions[-2]['Put']['ExpressionAttributeValues'],{':expected':{'N':'3'}})
        self.assertEqual(actions[-1]['Put']['ConditionExpression'],'attribute_not_exists(PK)')

    def test_conflicts_and_oversize_migrations_fail_before_writes(self):
        registry = empty_state(REGISTRY_ENTITIES)
        registry['users']['user#alice'] = {'id':'alice'}
        with self.assertRaises(ValueError):
            self.actions(registry)
        self.before['accounts'] = {'user#'+str(i):{'kind':'user','id':str(i)} for i in range(50)}
        with self.assertRaises(ValueError):
            self.actions()

    def test_noop_without_provider_records(self):
        self.before['accounts'] = {'business':{'type':'INTERNAL'}}
        self.assertEqual(self.actions(),[])

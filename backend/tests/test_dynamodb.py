"""Validate real boto3 request encoding and transaction limits without network calls."""
import copy
import unittest
from unittest.mock import patch

import boto3
from botocore.stub import Stubber, ANY

from backend.database import DynamoStore, StoreConflict, StoreLimitError, empty_state


class DynamoStorageTest(unittest.TestCase):
    def setUp(self):
        self.client = boto3.client('dynamodb', region_name='ap-south-2',
                                   aws_access_key_id='test', aws_secret_access_key='test')
        self.store = DynamoStore('test-ledger', client=self.client)

    def test_paginated_strongly_consistent_snapshot(self):
        key = self.store.encode({'PK':self.store.pk, 'SK':'META'})
        meta = self.store.encode({'PK':self.store.pk,'SK':'META','revision':3})
        row = self.store.encode({'PK':self.store.pk,'SK':'accounts#a','entity':'accounts','record_id':'a','data':{'id':'a','name':'Business'}})
        query = {'TableName':'test-ledger','KeyConditionExpression':'PK = :pk',
                 'ExpressionAttributeValues':{':pk':{'S':self.store.pk}},'ConsistentRead':True}
        with Stubber(self.client) as stub:
            stub.add_response('get_item',{'Item':meta},{'TableName':'test-ledger','Key':key,'ConsistentRead':True})
            stub.add_response('query',{'Items':[meta],'LastEvaluatedKey':key},query)
            stub.add_response('query',{'Items':[row]},{**query,'ExclusiveStartKey':key})
            stub.add_response('get_item',{'Item':meta},{'TableName':'test-ledger','Key':key,'ConsistentRead':True})
            revision,state = self.store.read()
            self.assertEqual(revision,3)
            self.assertEqual(state['accounts']['a']['name'],'Business')
            stub.assert_no_pending_responses()

    def test_atomic_data_and_revision_compare(self):
        before=empty_state(); after=copy.deepcopy(before)
        after['accounts']['a']={'id':'a','name':'Business'}
        expected = [
            {'Put':{'TableName':'test-ledger','Item':self.store.encode({'PK':self.store.pk,'SK':'accounts#a','entity':'accounts','record_id':'a','data':after['accounts']['a']})}},
            {'Put':{'TableName':'test-ledger','Item':self.store.encode({'PK':self.store.pk,'SK':'META','revision':8}),
                    'ConditionExpression':'#revision = :expected','ExpressionAttributeNames':{'#revision':'revision'},
                    'ExpressionAttributeValues':{':expected':{'N':'7'}}}},
        ]
        with Stubber(self.client) as stub:
            stub.add_response('transact_write_items',{}, {'TransactItems':expected,'ClientRequestToken':ANY})
            self.store.commit(7,before,after)
            stub.assert_no_pending_responses()

    def test_conflict_is_not_a_partial_success(self):
        before=empty_state(); after=copy.deepcopy(before)
        after['accounts']['a']={'id':'a','name':'Business'}
        with Stubber(self.client) as stub:
            stub.add_client_error('transact_write_items','TransactionCanceledException',
                                  modeled_fields={'CancellationReasons':[{'Code':'ConditionalCheckFailed'}]})
            with self.assertRaises(StoreConflict):
                self.store.commit(3,before,after)

    def test_over_limit_rejected_before_any_network_write(self):
        before=empty_state(); after=copy.deepcopy(before)
        for i in range(100):
            after['repayment_allocations'][str(i)]={'amount_paise':1}
        with Stubber(self.client):
            with self.assertRaises(StoreLimitError):
                self.store.commit(1,before,after)

    def test_idempotent_noop_does_not_write(self):
        with Stubber(self.client):
            self.store.commit(1,empty_state(),empty_state())

    def test_five_table_write_is_one_atomic_request(self):
        tables = {entity:'test-'+entity for entity in empty_state()}
        store = DynamoStore(tables, client=self.client)
        before = empty_state()
        after = copy.deepcopy(before)
        for entity in tables:
            after[entity]['record'] = {'amount_paise':100}
        with patch.object(self.client, 'transact_write_items', return_value={}) as write:
            store.commit(2,before,after)
        actions = write.call_args.kwargs['TransactItems']
        self.assertEqual(len(actions),6)
        self.assertEqual({a['Put']['TableName'] for a in actions},set(tables.values()))
        self.assertEqual(actions[-1]['Put']['TableName'],tables['accounts'])
        self.assertEqual(actions[-1]['Put']['ConditionExpression'],'#revision = :expected')

    def test_five_table_snapshot_queries_every_table(self):
        tables = {entity:'test-'+entity for entity in empty_state()}
        store = DynamoStore(tables, client=self.client)
        with patch.object(store,'revision',return_value=4), patch.object(self.client,'query',return_value={'Items':[]}) as query:
            revision,state=store.read()
        self.assertEqual(revision,4)
        self.assertEqual(state,empty_state())
        self.assertEqual({c.kwargs['TableName'] for c in query.call_args_list},set(tables.values()))
        self.assertTrue(all(c.kwargs['ConsistentRead'] for c in query.call_args_list))

    def test_unmigrated_legacy_records_fail_closed(self):
        tables = {entity:'test-'+entity for entity in empty_state()}
        store = DynamoStore(tables, client=self.client)
        item=store.encode({'entity':'vendors','record_id':'v','data':{'name':'Supplier'}})
        with patch.object(store,'revision',return_value=4), patch.object(self.client,'query',return_value={'Items':[item]}):
            with self.assertRaisesRegex(StoreConflict,'migration'):
                store.read()


if __name__ == '__main__':
    unittest.main()

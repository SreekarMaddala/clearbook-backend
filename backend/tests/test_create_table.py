import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import boto3
from botocore.stub import Stubber

spec = importlib.util.spec_from_file_location('create_table', Path(__file__).resolve().parents[2] / 'scripts' / 'create-table.py')
creation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(creation)


class TableCreationTest(unittest.TestCase):
    def setUp(self):
        self.client = boto3.client('dynamodb', region_name='ap-south-2',
                                   aws_access_key_id='test', aws_secret_access_key='test')
        self.table = {'TableStatus': 'ACTIVE', 'KeySchema': creation.KEYS, 'AttributeDefinitions': creation.ATTRIBUTES}

    def test_existing_table_is_read_only(self):
        with Stubber(self.client) as stub:
            stub.add_response('describe_table', {'Table': self.table}, {'TableName': 'ledger'})
            self.assertFalse(creation.ensure_table(self.client, 'ledger'))
            stub.assert_no_pending_responses()

    def test_incompatible_existing_table_is_rejected(self):
        with Stubber(self.client) as stub:
            stub.add_response('describe_table', {'Table': {'KeySchema': [{'AttributeName': 'id', 'KeyType': 'HASH'}]}})
            with self.assertRaisesRegex(ValueError, 'incompatible'):
                creation.ensure_table(self.client, 'ledger')

    def test_dry_run_never_creates_or_seeds(self):
        with Stubber(self.client) as stub:
            stub.add_client_error('describe_table', 'ResourceNotFoundException')
            self.assertFalse(creation.ensure_table(self.client, 'ledger', dry_run=True))
            stub.assert_no_pending_responses()

    def test_creation_matches_sam_and_enables_recovery(self):
        waiter = Mock()
        with patch.object(self.client, 'get_waiter', return_value=waiter), Stubber(self.client) as stub:
            stub.add_client_error('describe_table', 'ResourceNotFoundException')
            stub.add_response('create_table', {}, {'TableName':'ledger', 'KeySchema':creation.KEYS,
                              'AttributeDefinitions':creation.ATTRIBUTES, 'BillingMode':'PAY_PER_REQUEST',
                              'SSESpecification':{'Enabled':True}, 'Tags':[{'Key':'Application','Value':'clearbook'}]})
            stub.add_response('describe_table', {'Table': self.table})
            stub.add_response('update_continuous_backups', {}, {'TableName':'ledger', 'PointInTimeRecoverySpecification':{'PointInTimeRecoveryEnabled':True}})
            self.assertTrue(creation.ensure_table(self.client, 'ledger'))
            waiter.wait.assert_called_once_with(TableName='ledger', WaiterConfig={'Delay':3, 'MaxAttempts':100})
            stub.assert_no_pending_responses()


if __name__ == '__main__':
    unittest.main()

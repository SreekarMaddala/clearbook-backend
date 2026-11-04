import concurrent.futures
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import boto3
from botocore.stub import Stubber

from backend.api import lambda_response
from backend.database import PLATFORM_WORKSPACE, connection, initialize, workspace_scope


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {'LOCAL_DATA_PATH':str(Path(self.temp.name) / 'test.json'), 'USER_POOL_ID':'ap-south-2_test'})
        self.environment.start()
        initialize()
        self.provider = {'sub':'provider', 'token_use':'access', 'scope':'clearbook/ledger', 'cognito:groups':'[SUPER_ADMIN]'}
        self.a = self.call('POST', '/provider/customers', {'name':'Organization A'})[1]
        self.b = self.call('POST', '/provider/customers', {'name':'Organization B'})[1]
        with workspace_scope(PLATFORM_WORKSPACE), connection(write=True) as db:
            for subject, organization in [('alice', self.a), ('bob', self.b)]:
                db.put('accounts', 'user#' + subject, {'id':subject, 'kind':'user', 'email':subject+'@example.com', 'customer_id':organization['id'], 'enabled':True})

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def call(self, method, path, body=None, subject=None, group='reference', query=None):
        claims = self.provider if subject is None else {'sub':subject, 'token_use':'access', 'scope':'clearbook/ledger'}
        event = {'rawPath':'/api'+path, 'body':json.dumps(body or {}), 'queryStringParameters':query, 'headers':{'Idempotency-Key':'test-request'}, 'requestContext':{'http':{'method':method}, 'authorizer':{'jwt':{'claims':claims}}}}
        response = lambda_response(event, group)
        return response['statusCode'], json.loads(response['body'])

    def test_provider_session_and_ledger_denial(self):
        status, session = self.call('GET', '/session')
        self.assertEqual(status, 200)
        self.assertEqual(session['role'], 'provider')
        for method, path, group in [('GET','/accounts','reference'), ('POST','/accounts','reference'), ('GET','/transactions','transactions'), ('POST','/transactions','transactions'), ('GET','/dashboard','dashboard')]:
            self.assertEqual(self.call(method,path,group=group)[0],403)

    def test_customer_cannot_manage_organizations(self):
        for method, path in [('GET','/provider/customers'), ('POST','/provider/customers'), ('GET','/provider/users'), ('POST','/provider/users')]:
            self.assertEqual(self.call(method,path,subject='alice')[0],403)

    def test_unassigned_user_is_denied(self):
        self.assertEqual(self.call('GET','/session',subject='unknown')[0],403)
        self.assertEqual(self.call('GET','/accounts',subject='unknown')[0],403)

    def test_org_ledgers_are_isolated_and_ignore_client_tenant(self):
        body={'name':'Private account','owner':'A','type':'INTERNAL','workspace_id':self.b['id']}
        status, account=self.call('POST','/accounts',body,subject='alice')
        self.assertEqual(status,201)
        self.assertEqual(self.call('GET','/accounts',subject='bob')[1]['items'],[])
        rows=self.call('GET','/accounts',subject='alice',query={'workspace_id':self.b['id']})[1]['items']
        self.assertEqual([row['id'] for row in rows],[account['id']])
        self.assertEqual(self.call('PATCH','/accounts/'+account['id'],{'name':'Stolen'},subject='bob')[0],404)
        self.assertEqual(self.call('GET','/session',subject='alice')[1]['workspace']['name'],'Organization A')

    def test_new_org_has_its_own_categories(self):
        self.assertEqual(len(self.call('GET','/categories',subject='alice')[1]['items']),11)
        self.assertEqual(self.call('GET','/accounts',subject='alice')[1]['items'],[])
        users=self.call('GET','/provider/users')[1]['items']
        self.assertEqual(len(users),2)
        self.assertEqual({user['customer_id'] for user in users},{self.a['id'],self.b['id']})

    def test_transactions_and_dashboard_totals_are_isolated(self):
        account=self.call('POST','/accounts',{'name':'A bank','owner':'A','type':'INTERNAL'},subject='alice')[1]
        status,transaction=self.call('POST','/transactions',{'type':'EXPENSE','date':'2026-11-04','amount':'123.45','payer':account['id'],'category':'Fuel','mode':'UPI','location':'Organization A site'},subject='alice',group='transactions')
        self.assertEqual(status,201,transaction)
        self.assertEqual(self.call('GET','/transactions',subject='bob',group='transactions')[1]['items'],[])
        self.assertEqual(self.call('PATCH','/transactions/'+transaction['id'],{'version':1,'amount':'1'},subject='bob',group='transactions')[0],404)
        self.assertEqual(self.call('GET','/dashboard',subject='alice',group='dashboard')[1]['totals']['expenses'],123.45)
        self.assertEqual(self.call('GET','/dashboard',subject='bob',group='dashboard')[1]['totals']['expenses'],0)

    def test_disabling_an_org_blocks_its_users(self):
        self.assertEqual(self.call('PATCH','/provider/customers/'+self.a['id'],{'enabled':False})[0],200)
        self.assertEqual(self.call('GET','/accounts',subject='alice')[0],403)
        self.assertEqual(self.call('GET','/accounts',subject='bob')[0],200)

    def test_disabling_a_user_blocks_existing_tokens(self):
        self.call('PATCH','/provider/users/alice',{'enabled':False})
        self.assertEqual(self.call('GET','/session',subject='alice')[0],403)
        self.call('PATCH','/provider/users/alice',{'enabled':True})
        self.assertEqual(self.call('GET','/session',subject='alice')[0],200)

    def test_concurrent_requests_keep_org_context(self):
        def write(subject):
            return self.call('POST','/accounts',{'name':subject,'owner':subject,'type':'INTERNAL'},subject=subject)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(write,['alice','bob']))
        self.assertEqual([status for status,_ in results],[201,201])
        for subject in ('alice','bob'):
            self.assertEqual([row['name'] for row in self.call('GET','/accounts',subject=subject)[1]['items']],[subject])

    def test_two_user_limit_is_per_org(self):
        with workspace_scope(PLATFORM_WORKSPACE), connection(write=True) as db:
            db.put('accounts','user#alice-2',{'id':'alice-2','kind':'user','email':'second@example.com','customer_id':self.a['id'],'enabled':True})
        too_many={'email':'third@example.com','customer_id':self.a['id'],'temporary_password':'Temporary-Test-123'}
        self.assertEqual(self.call('POST','/provider/users',too_many)[0],409)
        other_org={**too_many,'customer_id':self.b['id'],'temporary_password':'short'}
        self.assertEqual(self.call('POST','/provider/users',other_org)[0],400)

    def test_provider_validation(self):
        self.assertEqual(self.call('POST','/provider/customers',{'name':'Organization A'})[0],409)
        self.assertEqual(self.call('POST','/provider/customers',{'name':''})[0],400)
        self.assertEqual(self.call('PATCH','/provider/customers/'+self.a['id'],{'enabled':'true'})[0],400)
        self.assertEqual(self.call('POST','/provider/users',{'email':'bad','customer_id':self.b['id']})[0],400)

    def test_cognito_onboarding_never_stores_password(self):
        client=boto3.client('cognito-idp',region_name='ap-south-2',aws_access_key_id='test',aws_secret_access_key='test')
        body={'email':'new@example.com','customer_id':self.a['id'],'temporary_password':'Temporary-Test-123'}
        with Stubber(client) as stub, patch('backend.provider.cognito_client',return_value=client):
            stub.add_response('admin_create_user',{'User':{'Username':'new-user','Attributes':[{'Name':'sub','Value':'new-sub'}],'UserStatus':'FORCE_CHANGE_PASSWORD'}},{'UserPoolId':'ap-south-2_test','Username':body['email'],'MessageAction':'SUPPRESS','TemporaryPassword':body['temporary_password'],'UserAttributes':[{'Name':'email','Value':body['email']}]})
            stub.add_response('admin_list_groups_for_user',{'Groups':[]},{'UserPoolId':'ap-south-2_test','Username':'new-user'})
            status,member=self.call('POST','/provider/users',body)
            self.assertEqual(status,201,member)
            stub.assert_no_pending_responses()
        self.assertEqual(self.call('GET','/session',subject='new-sub')[1]['workspace']['id'],self.a['id'])
        with workspace_scope(PLATFORM_WORKSPACE),connection() as db:
            self.assertNotIn(body['temporary_password'],json.dumps(db.state))
        self.assertNotIn('temporary_password',member)

    def test_provider_cannot_be_enrolled_as_org_user(self):
        client=boto3.client('cognito-idp',region_name='ap-south-2',aws_access_key_id='test',aws_secret_access_key='test')
        body={'email':'provider@example.com','customer_id':self.a['id'],'temporary_password':'Temporary-Test-123'}
        with Stubber(client) as stub,patch('backend.provider.cognito_client',return_value=client):
            stub.add_client_error('admin_create_user','UsernameExistsException')
            stub.add_response('admin_get_user',{'Username':'provider','UserAttributes':[{'Name':'sub','Value':'provider'}]})
            stub.add_response('admin_list_groups_for_user',{'Groups':[{'GroupName':'SUPER_ADMIN'}]})
            self.assertEqual(self.call('POST','/provider/users',body)[0],409)
            stub.assert_no_pending_responses()


if __name__ == '__main__':
    unittest.main()

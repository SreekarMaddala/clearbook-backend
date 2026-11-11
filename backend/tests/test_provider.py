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
                db.put('users', 'user#' + subject, {'id':subject, 'kind':'user', 'email':subject+'@example.com', 'customer_id':organization['id'], 'enabled':True})

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
        self.assertEqual(len(self.call('GET','/categories',subject='alice')[1]['items']),12)
        self.assertEqual(self.call('GET','/accounts',subject='alice')[1]['items'],[])
        users=self.call('GET','/provider/users')[1]['items']
        self.assertEqual(len(users),2)
        self.assertEqual({user['customer_id'] for user in users},{self.a['id'],self.b['id']})

    def test_custom_categories_are_validated_and_isolated(self):
        status, category = self.call('POST','/categories',{'name':' Office supplies '},subject='alice')
        self.assertEqual(status,201)
        self.assertEqual(category['name'],'Office supplies')
        self.assertIn('Office supplies',[c['name'] for c in self.call('GET','/categories',subject='alice')[1]['items']])
        self.assertNotIn('Office supplies',[c['name'] for c in self.call('GET','/categories',subject='bob')[1]['items']])
        self.assertEqual(self.call('POST','/categories',{'name':'office SUPPLIES'},subject='alice')[0],409)
        for name in ['', '   ', 'a'*81, None]:
            self.assertEqual(self.call('POST','/categories',{'name':name},subject='alice')[0],400)
        self.assertEqual(self.call('POST','/categories',{'name':'Fuel'},subject='alice')[0],409)
        self.assertEqual(self.call('POST','/categories',{'name':'Office supplies'},subject='bob')[0],201)

    def test_transactions_and_dashboard_totals_are_isolated(self):
        account=self.call('POST','/accounts',{'name':'A bank','owner':'A','type':'INTERNAL'},subject='alice')[1]
        status,transaction=self.call('POST','/transactions',{'type':'EXPENSE','date':'2026-11-04','amount':'123.45','payer':account['id'],'category':'Fuel','mode':'UPI','location':'Organization A site'},subject='alice',group='transactions')
        self.assertEqual(status,201,transaction)
        self.assertEqual(self.call('GET','/transactions',subject='bob',group='transactions')[1]['items'],[])
        self.assertEqual(self.call('PATCH','/transactions/'+transaction['id'],{'version':1,'amount':'1'},subject='bob',group='transactions')[0],404)
        self.assertEqual(self.call('GET','/dashboard',subject='alice',group='dashboard')[1]['totals']['expenses'],123.45)
        self.assertEqual(self.call('GET','/dashboard',subject='bob',group='dashboard')[1]['totals']['expenses'],0)

    def test_clients_and_credits_are_isolated(self):
        account = self.call('POST','/accounts',{'name':'Bank','owner':'A','type':'INTERNAL'},subject='alice')[1]
        client = self.call('POST','/clients',{'name':'ABC','workspace_id':self.b['id']},subject='alice')[1]
        self.assertEqual(self.call('GET','/clients',subject='bob')[1]['items'],[])
        self.assertEqual(self.call('PATCH','/clients/'+client['id'],{'name':'Stolen'},subject='bob')[0],404)
        body = {'type':'CREDIT','date':'2026-11-04','amount':'100','client_id':client['id'],
                'recipient':account['id'],'mode':'UPI','location':'Office'}
        self.assertEqual(self.call('POST','/transactions',body,subject='alice',group='transactions')[0],201)
        self.assertEqual(self.call('POST','/transactions',body,subject='bob',group='transactions')[0],404)
        self.assertEqual(self.call('GET','/dashboard',subject='alice',group='dashboard')[1]['totals']['credits'],100)
        self.assertEqual(self.call('GET','/dashboard',subject='bob',group='dashboard')[1]['totals']['credits'],0)
        self.assertEqual(self.call('POST','/clients',{'name':'Provider client'})[0],403)

    def test_locations_are_isolated(self):
        self.assertEqual(self.call('POST','/locations',{'name':'Private site'},subject='alice')[0],201)
        self.assertEqual(self.call('GET','/locations',subject='alice')[1]['items'],[{'name':'Private site'}])
        self.assertEqual(self.call('GET','/locations',subject='bob')[1]['items'],[])
        self.assertEqual(self.call('POST','/locations',{'name':'Private site'},subject='bob')[0],201)
        self.assertEqual(self.call('POST','/locations',{'name':'Private site'})[0],403)

    def test_registry_is_separate_from_business_accounts(self):
        with workspace_scope(PLATFORM_WORKSPACE), connection() as db:
            self.assertEqual(db.all('accounts'),[])
            self.assertEqual(len(db.all('organizations')),2)
            self.assertEqual(len(db.all('users')),2)
        self.assertEqual(self.call('GET','/accounts',subject='alice')[1]['items'],[])

    def test_legacy_local_registry_documents_are_preserved(self):
        with workspace_scope(PLATFORM_WORKSPACE), connection(write=True) as db:
            db.put('accounts','customer#legacy',{'id':'legacy','kind':'customer','name':'Legacy','enabled':True})
            db.put('accounts','user#legacy-user',{'id':'legacy-user','kind':'user','email':'legacy@example.com','customer_id':'legacy','enabled':True})
        self.assertEqual(self.call('GET','/session',subject='legacy-user')[0],200)
        with workspace_scope(PLATFORM_WORKSPACE), connection(write=True) as db:
            self.assertEqual(db.get('organizations','customer#legacy')['name'],'Legacy')
            self.assertEqual(db.get('users','user#legacy-user')['email'],'legacy@example.com')
            self.assertEqual(db.all('accounts'),[])

    def test_delete_user_revokes_access_and_preserves_tombstone(self):
        self.assertEqual(self.call('DELETE','/provider/users/alice',subject='bob')[0],403)
        self.assertEqual(self.call('DELETE','/provider/users/alice')[0],200)
        self.assertEqual(self.call('GET','/session',subject='alice')[0],403)
        users = self.call('GET','/provider/users')[1]['items']
        self.assertEqual([u['id'] for u in users],['bob'])
        self.assertEqual(self.call('PATCH','/provider/users/alice',{'enabled':True})[0],404)
        with workspace_scope(PLATFORM_WORKSPACE), connection() as db:
            self.assertTrue(db.get('users','user#alice')['deleted_at'])
            self.assertFalse(db.get('users','user#alice')['enabled'])

    def test_delete_organization_revokes_its_users_and_preserves_ledger(self):
        account = self.call('POST','/accounts',{'name':'Bank','owner':'A','type':'INTERNAL'},subject='alice')[1]
        self.assertEqual(self.call('DELETE','/provider/customers/'+self.a['id'])[0],200)
        self.assertEqual(self.call('GET','/session',subject='alice')[0],403)
        self.assertEqual(self.call('GET','/session',subject='bob')[0],200)
        self.assertEqual([c['id'] for c in self.call('GET','/provider/customers')[1]['items']],[self.b['id']])
        with workspace_scope(self.a['id']), connection() as db:
            self.assertEqual(db.get('accounts',account['id'])['name'],'Bank')
        with workspace_scope(PLATFORM_WORKSPACE), connection() as db:
            self.assertTrue(db.get('users','user#alice')['deleted_at'])

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
            db.put('users','user#alice-2',{'id':'alice-2','kind':'user','email':'second@example.com','customer_id':self.a['id'],'enabled':True})
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

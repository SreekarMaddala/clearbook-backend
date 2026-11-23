import concurrent.futures
import json
import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from backend.api import ROUTES, dispatch, lambda_response
from backend.database import PLATFORM_WORKSPACE, connection, initialize, workspace_scope
from backend.reference_handler import handler as reference_handler
from backend.transactions_handler import handler as transactions_handler
from backend.dashboard_handler import handler as dashboard_handler


class APITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_url = os.environ.get('LOCAL_DATA_PATH')
        os.environ['LOCAL_DATA_PATH'] = str(Path(self.temp.name) / 'test.json')
        initialize()
        with workspace_scope(PLATFORM_WORKSPACE), connection(write=True) as db:
            db.put('organizations', 'customer#clearbook', {'id':'clearbook', 'name':'Test customer', 'enabled':True})
            db.put('users', 'user#test-user', {'email':'test@example.com', 'customer_id':'clearbook', 'enabled':True})
        self.business = self.create('/accounts', {'name':'Business','type':'INTERNAL','owner':'Company'})
        self.creditor = self.create('/accounts', {'name':'Uncle','type':'THIRD_PARTY_CREDITOR','owner':'Uncle'})
        self.vendor = self.create('/vendors', {'name':'Shree Balaji Steels','category':'Other Expense','role':'Supplier'})

    def tearDown(self):
        if self.previous_url is None:
            os.environ.pop('LOCAL_DATA_PATH', None)
        else:
            os.environ['LOCAL_DATA_PATH'] = self.previous_url
        self.temp.cleanup()

    def call(self, method, path, body=None, **kwargs):
        return dispatch(method, '/api' + path, body, **kwargs)

    def create(self, path, body, key=None):
        status, result = self.call('POST', path, body, headers={'Idempotency-Key':key or str(uuid4())})
        self.assertEqual(status, 201, result)
        return result

    def expense(self, amount=25000, **changes):
        return {'date':'2026-11-01','amount':amount,'type':'EXPENSE','payer':self.creditor['id'],
                'vendor':self.vendor['id'],'category':'Other Expense','mode':'UPI','location':'Hyderabad - Block A','notes':'Steel delivery', **changes}

    def payback(self, amount=10000, **changes):
        return {'date':'2026-11-02','amount':amount,'type':'PAYBACK','payer':self.business['id'],
                'recipient':self.creditor['id'],'mode':'NEFT','location':'Hyderabad office', **changes}

    def test_client_credit_lifecycle_and_account_movement(self):
        client = self.create('/clients', {'name':'ABC', 'email':'abc@example.com'})
        body = {'date':'2026-11-04', 'amount':'500.25', 'type':'CREDIT',
                'client_id':client['id'], 'recipient':self.business['id'], 'mode':'UPI', 'location':'Office'}
        key = str(uuid4())
        credit = self.create('/transactions', body, key)
        self.assertEqual(self.create('/transactions', body, key)['id'], credit['id'])
        self.assertEqual(credit['status'], ['received','Received'])
        self.assertIsNone(credit['payer'])
        self.assertEqual(credit['allocations'], [])
        totals = self.call('GET','/dashboard')[1]
        self.assertEqual(totals['totals']['credits'],500.25)
        self.assertEqual(totals['totals']['expenses'],0)
        self.assertEqual(totals['totals']['outstanding'],0)
        self.assertEqual(totals['accounts'][0]['net_movement'],500.25)
        self.assertEqual(totals['clients'][0]['received'],500.25)
        rows = self.call('GET','/transactions',query={'client_id':client['id'],'account':self.business['id']})[1]['items']
        self.assertEqual([t['id'] for t in rows],[credit['id']])
        self.assertEqual(len(self.call('GET','/transactions',query={'q':'ABC'})[1]['items']),1)
        status, updated = self.call('PATCH','/transactions/'+credit['id'],{'amount':'700','version':1})
        self.assertEqual(status,200,updated)
        self.assertEqual(updated['amount'],700)
        self.assertEqual(self.call('PATCH','/transactions/'+credit['id'],{'amount':'900','version':1})[0],409)
        self.assertEqual(self.call('POST','/transactions/'+credit['id']+'/reverse',{'reason':'Test','version':2})[0],200)
        self.assertEqual(self.call('GET','/dashboard')[1]['totals']['credits'],0)
        self.assertEqual(self.call('GET','/dashboard')[1]['accounts'][0]['net_movement'],0)

    def test_credit_and_client_validation(self):
        client = self.create('/clients', {'name':'ABC'})
        self.assertEqual(self.call('POST','/clients',{'name':' abc '})[0],409)
        self.assertEqual(self.call('POST','/clients',{'name':'Invalid','email':'bad'})[0],400)
        status, updated = self.call('PATCH','/clients/'+client['id'],{'contact':'A person'})
        self.assertEqual(status,200)
        self.assertEqual(updated['name'],'ABC')
        self.assertEqual(updated['contact'],'A person')
        body = {'date':'2026-11-04','amount':'500','type':'CREDIT','client_id':client['id'],
                'recipient':self.business['id'],'mode':'UPI','location':'Office'}
        for changes in ({'client_id':None},{'client_id':'missing'},{'recipient':self.creditor['id']},
                        {'payer':self.business['id']},{'vendor':self.vendor['id']},{'category':'Fuel'},
                        {'amount':0},{'amount':'1.001'},{'recipient':None}):
            self.assertIn(self.call('POST','/transactions',{**body,**changes})[0],(400,404))
        self.assertEqual(self.call('POST','/transactions',self.expense(client_id=client['id']))[0],400)

    def test_existing_local_store_without_clients_is_upgraded(self):
        path = Path(os.environ['LOCAL_DATA_PATH'])
        state = json.loads(path.read_text(encoding='utf-8'))
        state.pop('clients', None)
        path.write_text(json.dumps(state),encoding='utf-8')
        self.assertEqual(self.call('GET','/clients')[1]['items'],[])
        self.create('/clients',{'name':'New client'})
        updated = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(updated['accounts'],state['accounts'])
        self.assertEqual(updated['vendors'],state['vendors'])
        self.assertEqual(len(updated['clients']),1)

    def test_saved_and_historical_locations(self):
        self.assertEqual(self.call('GET','/locations')[1]['items'],[])
        saved = self.create('/locations',{'name':' New site '})
        self.assertEqual(saved,{'name':'New site'})
        self.assertEqual(self.call('POST','/locations',{'name':'new SITE'})[0],409)
        for name in ['', '   ', 'x'*201, None]:
            self.assertEqual(self.call('POST','/locations',{'name':name})[0],400)
        self.create('/transactions',self.expense(location='Historical site'))
        self.assertEqual(self.call('GET','/locations')[1]['items'],[{'name':'Historical site'},{'name':'New site'}])
        self.assertEqual(self.call('POST','/locations',{'name':'historical SITE'})[0],409)
        self.assertEqual(len(self.call('GET','/categories')[1]['items']),12)

    def summary(self):
        status, result = self.call('GET', '/dashboard')
        self.assertEqual(status, 200)
        return result

    def test_staff_roles_and_salary_payment_lifecycle(self):
        staff = self.create('/vendors', {'name': 'Supervisor', 'category': 'Staff Salaries',
                                        'role': 'Site supervisor', 'roles': ['STAFF', 'SUPPLIER']})
        self.assertEqual(staff['roles'], ['SUPPLIER', 'STAFF'])
        body = self.expense(vendor=staff['id'], category='Staff Salaries', salary_month='2026-10')
        payment = self.create('/transactions', body)
        self.assertEqual(payment['salary_month'], '2026-10')
        status, rows = self.call('GET', '/transactions', query={'salary_month': '2026-10'})
        self.assertEqual([r['id'] for r in rows['items']], [payment['id']])
        self.assertEqual(self.call('PATCH', '/vendors/' + staff['id'], {'roles': ['SUPPLIER'], 'category': 'Fuel'})[0], 409)
        status, updated = self.call('PATCH', '/transactions/' + payment['id'],
                                    {'salary_month': '2026-09', 'version': payment['version']})
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated['salary_month'], '2026-09')
        self.assertEqual(updated['amount'], payment['amount'])
        status, changed = self.call('PATCH', '/transactions/' + payment['id'],
                                    {'category': 'Other Expense', 'version': updated['version']})
        self.assertEqual(status, 200, changed)
        self.assertNotIn('salary_month', changed)
        self.assertEqual(self.call('POST', '/transactions/' + payment['id'] + '/reverse',
                                  {'reason': 'Test reversal', 'version': changed['version']})[0], 200)

    def test_salary_requires_staff_and_valid_optional_month(self):
        staff = self.create('/vendors', {'name': 'Employee', 'category': 'Staff Salaries', 'roles': ['STAFF']})
        salary = self.expense(vendor=staff['id'], category='Staff Salaries', salary_month='2026-10')
        for changes in ({'salary_month': '2026-13'}, {'salary_month': '2026-1'},
                        {'vendor': self.vendor['id']}, {'vendor': None}, {'category': 'Fuel'}):
            self.assertIn(self.call('POST', '/transactions', {**salary, **changes},
                                    headers={'Idempotency-Key': str(uuid4())})[0], (400, 404))
        for month_fields in ({}, {'salary_month': None}):
            body = self.expense(vendor=staff['id'], category='Staff Salaries', **month_fields)
            payment = self.create('/transactions', body)
            self.assertIsNone(payment.get('salary_month'))
            status, updated = self.call('PATCH', '/transactions/' + payment['id'],
                                        {'notes': 'Edited without month', 'version': payment['version']})
            self.assertEqual(status, 200, updated)
            self.assertIsNone(updated.get('salary_month'))
        for roles in ([], 'STAFF', ['UNKNOWN'], [None]):
            self.assertEqual(self.call('PATCH', '/vendors/' + staff['id'], {'roles': roles})[0], 400)
        self.assertEqual(self.call('PATCH', '/vendors/' + staff['id'], {'roles': ['SUPPLIER']})[0], 400)

    def test_legacy_payee_roles_and_non_salary_idempotency(self):
        with connection(write=True) as db:
            vendor = db.get('vendors', self.vendor['id'])
            vendor.pop('roles', None)
        rows = self.call('GET', '/vendors')[1]['items']
        self.assertEqual(rows[0]['roles'], ['SUPPLIER'])
        body = self.expense()
        payment = self.create('/transactions', body, key='legacy-retry')
        duplicate = self.create('/transactions', {**body, 'salary_month': None}, key='legacy-retry')
        self.assertEqual(payment['id'], duplicate['id'])

    def test_contract_and_categories(self):
        self.assertEqual(len(ROUTES), 18)
        status, data = self.call('GET', '/categories')
        self.assertEqual(len(data['items']), 12)
        self.assertEqual(self.call('GET', '/unknown')[0], 404)
        self.assertEqual(self.call('POST', '/categories', {})[0], 400)

    def test_required_location_and_real_date(self):
        for body in [self.expense(location=''), self.expense(location=None), self.expense(date='2026-02-30'),
                     self.expense(amount='NaN'), self.expense(amount='1.001'), self.expense(amount=True), self.expense(payer=[])]:
            self.assertEqual(self.call('POST','/transactions',body,headers={'Idempotency-Key':str(uuid4())})[0],400)
        self.assertEqual(self.summary()['totals']['expenses'], 0)

    def test_vendor_optional_and_filters(self):
        self.create('/transactions', self.expense())
        self.create('/transactions', self.expense(10, vendor=None,location='Chennai'))
        status, result = self.call('GET', '/transactions', query={'vendor':self.vendor['id'],'location':'Hyderabad - Block A','q':'steel','limit':'1'})
        self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['vendor'],self.vendor['id'])
        self.assertEqual(self.call('GET','/transactions',query={'page':'0'})[0],400)

    def test_partial_allocation_and_no_double_expense(self):
        expense = self.create('/transactions', self.expense())
        repayment = self.create('/transactions', self.payback())
        self.assertEqual(repayment['allocations'], [{'funding_id':expense['id'],'amount':10000}])
        summary = self.summary()
        self.assertEqual(summary['totals']['expenses'],25000)
        self.assertEqual(summary['totals']['outstanding'],15000)
        rows = self.call('GET','/transactions')[1]['items']
        self.assertEqual(next(t for t in rows if t['id']==expense['id'])['status'], ['partial','Partially paid'])

    def test_overpayment_and_backdating_rollback(self):
        self.create('/transactions', self.expense())
        for body in [self.payback(25001), self.payback(100,date='2026-10-31')]:
            self.assertEqual(self.call('POST','/transactions',body,headers={'Idempotency-Key':str(uuid4())})[0],409)
        self.assertEqual(self.summary()['totals']['settled'],0)

    def test_idempotency_and_different_payload(self):
        key = str(uuid4())
        first = self.create('/transactions',self.expense(),key)
        second = self.create('/transactions',self.expense(),key)
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(self.call('POST','/transactions',self.expense(100),headers={'Idempotency-Key':key})[0],409)
        self.assertEqual(self.summary()['totals']['expenses'],25000)

    def test_update_version_and_invalid_funding_change(self):
        expense = self.create('/transactions',self.expense())
        self.create('/transactions',self.payback())
        self.assertEqual(self.call('PATCH',f'/transactions/{expense["id"]}',{'amount':5000,'version':1})[0],409)
        updated = self.call('PATCH',f'/transactions/{expense["id"]}',{'amount':'30000.25','location':'Block B','version':1})
        self.assertEqual(updated[0],200,updated)
        self.assertEqual(updated[1]['version'],2)
        self.assertEqual(updated[1]['amount'],30000.25)
        self.assertEqual(self.call('PATCH',f'/transactions/{expense["id"]}',{'notes':'stale','version':1})[0],409)

    def test_reversal_history_and_allocation_rollback(self):
        expense = self.create('/transactions',self.expense())
        repayment = self.create('/transactions',self.payback())
        path = f'/transactions/{expense["id"]}/reverse'
        self.assertEqual(self.call('POST',path,{'reason':'Wrong entry','version':1})[0],409)
        self.assertEqual(self.summary()['totals']['outstanding'],15000)
        with connection() as db:
            self.assertEqual(len(db.all('repayment_allocations')),1)
        self.assertEqual(self.call('POST',f'/transactions/{repayment["id"]}/reverse',{'reason':'Wrong entry','version':1})[0],200)
        self.assertEqual(self.call('POST',path,{'reason':'Wrong entry','version':1})[0],200)
        self.assertEqual(self.call('GET','/transactions')[1]['total'],0)
        self.assertEqual(self.call('GET','/transactions',query={'include_reversed':'true'})[1]['total'],2)
        with connection() as db:
            original = db.get('transactions', expense['id'])
            self.assertTrue(original['reversed_at'])
            self.assertEqual(json.loads(original['history'])[-1]['action'],'reverse')

    def test_external_funding_transfer_allocation(self):
        funding = self.create('/transactions',{'date':'2026-11-01','amount':10000,'type':'TRANSFER',
                          'payer':self.creditor['id'],'recipient':self.business['id'],'mode':'IMPS','location':'Office'})
        repayment = self.create('/transactions',self.payback(5000))
        self.assertEqual(repayment['allocations'][0]['funding_id'],funding['id'])
        self.assertEqual(self.summary()['totals']['expenses'],0)
        self.assertEqual(self.summary()['totals']['outstanding'],5000)
        wrong = self.expense(type='TRANSFER',recipient=self.business['id'],payer=self.business['id'],category=None,vendor=None)
        self.assertEqual(self.call('POST','/transactions',wrong,headers={'Idempotency-Key':str(uuid4())})[0],400)

    def test_concurrent_repayments_cannot_overpay(self):
        self.create('/transactions',self.expense(10000))
        def repay(_):
            return self.call('POST','/transactions',self.payback(7000),headers={'Idempotency-Key':str(uuid4())})[0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(repay, range(2)))
        self.assertEqual(sorted(statuses),[201,409])
        self.assertEqual(self.summary()['totals']['outstanding'],3000)

    def test_reference_updates_and_type_lock(self):
        self.create('/transactions',self.expense())
        self.assertEqual(self.call('PATCH',f'/accounts/{self.creditor["id"]}',{'type':'INTERNAL'})[0],409)
        self.assertEqual(self.call('PATCH',f'/vendors/{self.vendor["id"]}',{'name':'Updated supplier'})[0],200)
        self.assertEqual(self.create('/transactions',self.expense())['vendor'],self.vendor['id'])
        self.assertEqual(self.call('POST','/vendors',{'name':'updated supplier','category':'Food'})[0],409)

    def test_lambda_envelope_and_group_isolation(self):
        claims={'sub':'test-user','token_use':'access','scope':'openid clearbook/ledger'}
        event={'rawPath':'/api/categories','requestContext':{'http':{'method':'GET'},'authorizer':{'jwt':{'claims':claims}}}}
        response=lambda_response(event,'reference')
        self.assertEqual(response['statusCode'],200)
        self.assertEqual(lambda_response(event,'dashboard')['statusCode'],404)
        self.assertEqual(reference_handler(event,None)['statusCode'],200)
        transaction_event={**event,'rawPath':'/api/transactions'}
        dashboard_event={**event,'rawPath':'/api/dashboard'}
        self.assertEqual(transactions_handler(transaction_event,None)['statusCode'],200)
        self.assertEqual(dashboard_handler(dashboard_event,None)['statusCode'],200)
        event['body']='not json'
        self.assertEqual(lambda_response(event,'reference')['statusCode'],400)
        event['body']='[]'
        self.assertEqual(lambda_response(event,'reference')['statusCode'],400)

    def test_lambda_rejects_missing_or_wrong_token(self):
        event={'rawPath':'/api/categories','requestContext':{'http':{'method':'GET'}}}
        self.assertEqual(reference_handler(event,None)['statusCode'],401)
        claims={'sub':'test-user','token_use':'id','scope':'clearbook/ledger'}
        event['requestContext']['authorizer']={'jwt':{'claims':claims}}
        self.assertEqual(reference_handler(event,None)['statusCode'],401)
        claims['token_use']='access'
        claims['scope']='openid'
        self.assertEqual(reference_handler(event,None)['statusCode'],403)

    def test_administrative_init_is_not_an_http_endpoint(self):
        self.assertTrue(reference_handler({'operation':'initialize_database'},None)['initialized'])
        event={'rawPath':'/api/categories','operation':'initialize_database','requestContext':{'http':{'method':'GET'}}}
        self.assertEqual(reference_handler(event,None)['statusCode'],401)


if __name__ == '__main__':
    unittest.main()

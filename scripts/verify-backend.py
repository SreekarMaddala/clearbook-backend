"""Verify the deployed handlers, JWT protection and DynamoDB atomic accounting.

Uses an isolated random DynamoDB workspace for write checks; removes only those
synthetic items afterwards. Never writes business data into the app workspace.
"""
import concurrent.futures
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.api import dispatch
from backend.database import DynamoStore, empty_state, initialize

REGION = 'ap-south-2'


def main():
    session = boto3.Session(region_name=REGION)
    stack = session.client('cloudformation').describe_stacks(StackName='clearbook-hyderabad')['Stacks'][0]
    outputs = {r['OutputKey']:r['OutputValue'] for r in stack['Outputs']}
    if outputs['AwsRegion'] != REGION:
        raise RuntimeError('Unexpected deployment region.')
    resources = session.client('cloudformation').list_stack_resources(StackName='clearbook-hyderabad')['StackResourceSummaries']
    assert sum(r['ResourceType']=='AWS::Lambda::Function' for r in resources) == 3
    assert sum(r['ResourceType']=='AWS::DynamoDB::Table' for r in resources) == 0  # Six existing external tables.
    assert not any(r['ResourceType'].startswith('AWS::RDS::') for r in resources)
    client = session.client('lambda')
    claims = {'sub':'deployment-verifier','token_use':'access','scope':'openid clearbook/ledger','cognito:groups':['SUPER_ADMIN']}
    for function, path in [('ReferenceFunctionName','/api/session'), ('TransactionsFunctionName','/api/transactions'), ('DashboardFunctionName','/api/dashboard')]:
        event = {'rawPath':path,'requestContext':{'http':{'method':'GET'},'authorizer':{'jwt':{'claims':claims}}}}
        response = client.invoke(FunctionName=outputs[function],Payload=json.dumps(event).encode())
        data = json.loads(response['Payload'].read())
        assert not response.get('FunctionError'), data
        assert data['statusCode'] == (200 if function=='ReferenceFunctionName' else 403), data
        if function=='ReferenceFunctionName':
            assert json.loads(data['body'])['role']=='provider'
    print('PASS: provider session resolves; transaction and dashboard handlers deny provider ledger access.')
    try:
        urlopen(outputs['ApiBaseUrl']+'/accounts',timeout=15)
        raise AssertionError('Unauthenticated request was accepted.')
    except HTTPError as error:
        assert error.code == 401, error.code
    request=Request(outputs['ApiBaseUrl']+'/accounts',method='OPTIONS',headers={
        'Origin':'http://localhost:5173','Access-Control-Request-Method':'GET',
        'Access-Control-Request-Headers':'authorization,content-type'})
    with urlopen(request,timeout=15) as response:
        assert response.headers.get('access-control-allow-origin')=='http://localhost:5173'
    print('PASS: live API rejects missing JWT; localhost CORS preflight succeeds.')
    api_id=outputs['ApiBaseUrl'].split('//')[1].split('.')[0]
    routes=session.client('apigatewayv2').get_routes(ApiId=api_id)['Items']
    assert len(routes)==27
    assert all(r.get('AuthorizationType')=='JWT' and 'clearbook/ledger' in r.get('AuthorizationScopes',[]) for r in routes)
    print('PASS: all 27 live routes require scoped JWT authentication.')
    tables={entity:outputs[key] for entity,key in {'organizations':'OrganizationsTableName','users':'UsersTableName','clients':'ClientsTableName','accounts':'AccountsTableName','vendors':'VendorsTableName','categories':'CategoriesTableName','transactions':'TransactionsTableName','repayment_allocations':'RepaymentAllocationsTableName'}.items()}
    assert len(set(tables.values()))==8
    for table in tables.values():
        assert session.client('dynamodb').describe_table(TableName=table)['Table']['TableStatus']=='ACTIVE'
    os.environ['DYNAMODB_TABLES']=json.dumps(tables)
    os.environ['AWS_REGION']=REGION
    os.environ['WORKSPACE_ID']='verification-'+uuid4().hex
    scratch = DynamoStore(tables)
    assert scratch.pk.startswith('WORKSPACE#verification-')
    def call(method,path,body=None,key=None):
        return dispatch(method,'/api'+path,body,headers={'Idempotency-Key':key or str(uuid4())})
    def create(path,body,key=None):
        status,data=call('POST',path,body,key)
        assert status==201,(status,data)
        return data
    try:
        initialize()
        business=create('/accounts',{'name':'Verification business','type':'INTERNAL','owner':'Verification'})
        creditor=create('/accounts',{'name':'Verification creditor','type':'THIRD_PARTY_CREDITOR','owner':'Verification'})
        vendor=create('/vendors',{'name':'Verification vendor','category':'Other Expense'})
        body={'date':'2026-11-01','amount':'25000.25','type':'EXPENSE','payer':creditor['id'],'vendor':vendor['id'],
              'category':'Other Expense','mode':'UPI','location':'Verification site','notes':'Synthetic deployment check'}
        key=str(uuid4())
        expense=create('/transactions',body,key)
        assert create('/transactions',body,key)['id']==expense['id']
        repayment={'date':'2026-11-02','amount':'10000','type':'PAYBACK','payer':business['id'],
                   'recipient':creditor['id'],'mode':'NEFT','location':'Verification office'}
        paid=create('/transactions',repayment)
        assert paid['allocations'][0]['funding_id']==expense['id']
        summary=call('GET','/dashboard')[1]['totals']
        assert summary['expenses']==25000.25 and summary['outstanding']==15000.25,summary
        assert call('POST','/transactions',{**repayment,'amount':'15001'})[0]==409
        assert call('PATCH','/transactions/'+expense['id'],{'version':1,'amount':'5000'})[0]==409
        assert call('POST','/transactions',{**body,'location':''})[0]==400
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(lambda _:call('POST','/transactions',repayment)[0],range(2)))
        assert sorted(statuses)==[201,409],statuses
        assert call('GET','/dashboard')[1]['totals']['outstanding']==5000.25
        print('PASS: real DynamoDB idempotency, partial repayment, decimal precision, rollback, validation and concurrent-write protection.')
    finally:
        # The random scratch PK is deliberately distinct from WORKSPACE#clearbook.
        revision,state=scratch.read()
        scratch.commit(revision,state,empty_state())
        scratch.client.delete_item(TableName=scratch.table,Key=scratch.encode({'PK':scratch.pk,'SK':'META'}),
                                   ConditionExpression='PK = :expected',ExpressionAttributeValues={':expected':{'S':scratch.pk}})
        print('Removed synthetic verification workspace; business workspace untouched.')
    print('API:',outputs['ApiBaseUrl'])


if __name__=='__main__':
    main()

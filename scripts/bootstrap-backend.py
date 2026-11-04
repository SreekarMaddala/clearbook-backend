"""Initialize DynamoDB categories through one existing Lambda; optionally set the admin password.

No invitations are sent. Passwords are read from masked interactive prompts,
passed through the SDK in memory, and never written to disk or shell arguments.
"""
import argparse
import getpass
import json
import re
import sys

REGION = 'ap-south-2'


def stack_outputs(session, name):
    stack = session.client('cloudformation').describe_stacks(StackName=name)['Stacks'][0]
    if stack['StackStatus'] not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE'):
        raise RuntimeError('The backend stack is not ready.')
    values = {item['OutputKey']:item['OutputValue'] for item in stack.get('Outputs', [])}
    if values.get('AwsRegion') != REGION:
        raise RuntimeError('This script only operates on the Hyderabad backend.')
    return values


def read_password():
    if not sys.stdin.isatty():
        raise RuntimeError('Run password setup from an interactive terminal so the password remains hidden.')
    password = getpass.getpass('Password (12+ chars, upper/lower/number/symbol): ')
    if password != getpass.getpass('Confirm password: '):
        raise ValueError('Passwords do not match.')
    if len(password) < 12 or not all(re.search(pattern, password) for pattern in (r'[a-z]', r'[A-Z]', r'[0-9]', r'[^a-zA-Z0-9]')):
        raise ValueError('Password does not meet the configured policy.')
    return password


def set_admin_password(session, outputs):
    email = outputs.get('SuperAdminEmail')
    if not email:
        raise RuntimeError('Deploy with SuperAdminEmail before setting the admin password.')
    password = read_password()
    session.client('cognito-idp').admin_set_user_password(UserPoolId=outputs['UserPoolId'], Username=email,
                                                         Password=password, Permanent=True)
    print('Super-admin password set. No invitation email sent.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stack', default='clearbook-hyderabad')
    parser.add_argument('--profile')
    parser.add_argument('--set-admin-password', action='store_true')
    args = parser.parse_args()
    import boto3
    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    outputs = stack_outputs(session, args.stack)
    response = session.client('lambda').invoke(FunctionName=outputs['ReferenceFunctionName'],
                                               Payload=json.dumps({'operation':'initialize_database'}).encode())
    payload = json.loads(response['Payload'].read())
    if response.get('FunctionError') or payload.get('initialized') is not True:
        raise RuntimeError('Database initialization failed; inspect the reference Lambda logs.')
    print('DynamoDB initialized; 11 categories ready.')
    if args.set_admin_password:
        set_admin_password(session, outputs)
    # Only public deployment identifiers are printed. No database URL or passwords.
    for key in ('AwsRegion','ApiBaseUrl','AccountsTableName','VendorsTableName','CategoriesTableName','TransactionsTableName','RepaymentAllocationsTableName','UserPoolId','UserPoolClientId','CognitoDomain','AuthRedirectUri','SuperAdminEmail'):
        print(f'{key}: {outputs[key]}')


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit('Setup cancelled. No credentials were written to disk.')

"""Provider onboarding for isolated organizations and their users."""
import os
import re
from uuid import uuid4

try:
    from .database import PLATFORM_WORKSPACE, connection, initialize, workspace_scope
    from .service import APIError, now, text
except ImportError:
    from database import PLATFORM_WORKSPACE, connection, initialize, workspace_scope
    from service import APIError, now, text

MAX_USERS_PER_ORGANIZATION = 2


def cognito_client():
    import boto3
    return boto3.client('cognito-idp', region_name=os.environ.get('AWS_REGION', 'ap-south-2'))


def organization_record(db, organization_id):
    if not isinstance(organization_id, str):
        raise APIError('Choose an organization.')
    organization = db.get('accounts', 'customer#' + organization_id)
    if not organization:
        raise APIError('Organization not found.', 404)
    return organization


def create_user(body):
    email = text(body.get('email'), 'Email', 254).lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise APIError('Enter a valid email address.')
    organization_id = body.get('customer_id')
    with workspace_scope(PLATFORM_WORKSPACE), connection() as db:
        organization = organization_record(db, organization_id)
        if not organization['enabled']:
            raise APIError('Enable the organization before adding users.', 409)
        organization_users = [u for u in db.all('accounts')
                              if u.get('kind') == 'user' and u.get('customer_id') == organization_id]
        if len(organization_users) >= MAX_USERS_PER_ORGANIZATION:
            raise APIError('An organization can have at most two customer users.', 409)
        existing = next((u for u in db.all('accounts') if u.get('kind') == 'user' and u['email'] == email), None)
        if existing:
            raise APIError('This user is already assigned to an organization.', 409)
    password = body.get('temporary_password')
    if not isinstance(password, str) or not 12 <= len(password) <= 256 or not all(
            re.search(pattern, password) for pattern in (r'[a-z]', r'[A-Z]', r'[0-9]', r'[^a-zA-Z0-9]')):
        raise APIError('Use a temporary password with 12+ characters, uppercase, lowercase, a number and a symbol.')
    pool = os.environ.get('USER_POOL_ID')
    if not pool:
        raise APIError('User provisioning requires the deployed Cognito configuration.', 503)
    client = cognito_client()
    existing_account = False
    try:
        result = client.admin_create_user(
            UserPoolId=pool, Username=email, MessageAction='SUPPRESS', TemporaryPassword=password,
            UserAttributes=[{'Name': 'email', 'Value': email}])
    except client.exceptions.UsernameExistsException:
        existing_account = True
        result = {'User': client.admin_get_user(UserPoolId=pool, Username=email)}
    user = result['User']
    attributes = user.get('Attributes', user.get('UserAttributes', []))
    subject = next(a['Value'] for a in attributes if a['Name'] == 'sub')
    groups = client.admin_list_groups_for_user(UserPoolId=pool, Username=user['Username'])['Groups']
    if any(g['GroupName'] == 'SUPER_ADMIN' for g in groups):
        raise APIError('A software-provider account cannot be assigned as a customer user.', 409)
    with workspace_scope(PLATFORM_WORKSPACE), connection(write=True) as db:
        organization = organization_record(db, organization_id)
        if not organization['enabled']:
            raise APIError('The organization was disabled. Enable it before retrying.', 409)
        organization_users = [u for u in db.all('accounts')
                              if u.get('kind') == 'user' and u.get('customer_id') == organization_id]
        if len(organization_users) >= MAX_USERS_PER_ORGANIZATION:
            raise APIError('An organization can have at most two customer users.', 409)
        if db.get('accounts', 'user#' + subject):
            raise APIError('This user is already assigned to an organization.', 409)
        member = {'id': subject, 'kind': 'user', 'username': user['Username'], 'email': email,
                  'customer_id': organization_id, 'enabled': True, 'created_at': now()}
        db.put('accounts', 'user#' + subject, member)
    return {**member, 'existing_account': existing_account}


def dispatch_provider(method, path, body):
    if path == '/provider/users' and method == 'POST':
        return 201, create_user(body)
    with workspace_scope(PLATFORM_WORKSPACE), connection(write=method != 'GET') as db:
        if path in ('/provider/customers', '/provider/users') and method == 'GET':
            kind = 'customer' if path.endswith('customers') else 'user'
            rows = [r for r in db.all('accounts') if r.get('kind') == kind]
            return 200, {'items': sorted(rows, key=lambda r: r.get('name', r.get('email', '')).casefold())}
        if path == '/provider/customers' and method == 'POST':
            name = text(body.get('name'), 'Organization name')
            if any(c.get('kind') == 'customer' and c['name'].casefold() == name.casefold() for c in db.all('accounts')):
                raise APIError('That organization already exists.', 409)
            organization_id = 'customer-' + uuid4().hex
            with workspace_scope(organization_id):
                initialize()
            organization = {'id': organization_id, 'kind': 'customer', 'name': name,
                            'enabled': True, 'created_at': now()}
            db.put('accounts', 'customer#' + organization_id, organization)
            return 201, organization
        match = re.fullmatch(r'/provider/(customers|users)/([^/]+)', path)
        if match and method == 'PATCH':
            collection, record_id = match.groups()
            kind = 'customer' if collection == 'customers' else 'user'
            record = db.get('accounts', kind + '#' + record_id)
            if not record:
                raise APIError('Record not found.', 404)
            if not isinstance(body.get('enabled'), bool):
                raise APIError('Enabled must be true or false.')
            record = {**record, 'enabled': body['enabled']}
            db.put('accounts', kind + '#' + record_id, record)
            return 200, record
    raise APIError('Endpoint not found.', 404)

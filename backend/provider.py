"""Provider operations. Registry metadata lives outside all customer ledgers."""
import os
import re
from uuid import uuid4

try:
    from .database import PLATFORM_WORKSPACE, connection, initialize, workspace_scope
    from .service import APIError, now, text
except ImportError:
    from database import PLATFORM_WORKSPACE, connection, initialize, workspace_scope
    from service import APIError, now, text


def cognito_client():
    import boto3
    return boto3.client('cognito-idp', region_name=os.environ.get('AWS_REGION', 'ap-south-2'))


def customer_record(db, customer_id):
    if not isinstance(customer_id, str):
        raise APIError('Choose a customer.')
    customer = db.get('accounts', 'customer#' + customer_id)
    if not customer:
        raise APIError('Customer not found.', 404)
    return customer


def create_user(body):
    email = text(body.get('email'), 'Email', 254).lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise APIError('Enter a valid email address.')
    customer_id = body.get('customer_id')
    with workspace_scope(PLATFORM_WORKSPACE), connection() as db:
        customer = customer_record(db, customer_id)
        if not customer['enabled']:
            raise APIError('Enable the customer before adding users.', 409)
        existing = next((u for u in db.all('accounts') if u.get('kind') == 'user' and u['email'] == email), None)
        if existing:
            raise APIError('This user is already assigned to a customer.', 409)
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
        # Recover a previous incomplete provisioning attempt without resetting passwords.
        existing_account = True
        result = {'User': client.admin_get_user(UserPoolId=pool, Username=email)}
    user = result['User']
    attributes = user.get('Attributes', user.get('UserAttributes', []))
    subject = next(a['Value'] for a in attributes if a['Name'] == 'sub')
    groups = client.admin_list_groups_for_user(UserPoolId=pool, Username=user['Username'])['Groups']
    if any(g['GroupName'] == 'SUPER_ADMIN' for g in groups):
        raise APIError('A software-provider account cannot be assigned to a customer.', 409)
    with workspace_scope(PLATFORM_WORKSPACE), connection(write=True) as db:
        customer = customer_record(db, customer_id)
        if not customer['enabled']:
            raise APIError('The customer was disabled. Enable it before retrying.', 409)
        existing = db.get('accounts', 'user#' + subject)
        if existing:
            raise APIError('This user is already assigned to a customer.', 409)
        member = {'id': subject, 'kind': 'user', 'username': user['Username'], 'email': email,
                  'customer_id': customer_id, 'enabled': True, 'created_at': now()}
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
            name = text(body.get('name'), 'Customer name')
            if any(c.get('kind') == 'customer' and c['name'].casefold() == name.casefold() for c in db.all('accounts')):
                raise APIError('That customer name already exists.', 409)
            customer_id = 'customer-' + uuid4().hex
            # Initialize only the new partition. Existing business data is never reassigned.
            with workspace_scope(customer_id):
                initialize()
            customer = {'id': customer_id, 'kind': 'customer', 'name': name, 'enabled': True, 'created_at': now()}
            db.put('accounts', 'customer#' + customer_id, customer)
            return 201, customer
        match = re.fullmatch(r'/provider/(customers|users)/([^/]+)', path)
        if match and method == 'PATCH':
            collection, record_id = match.groups()
            kind = 'customer' if collection == 'customers' else 'user'
            record = db.get('accounts', kind + '#' + record_id)
            if not record:
                raise APIError('Record not found.', 404)
            if not isinstance(body.get('enabled'), bool):
                raise APIError('Enabled must be true or false.')
            # Membership checks reject disabled users immediately, including existing tokens.
            record = {**record, 'enabled': body['enabled']}
            db.put('accounts', kind + '#' + record_id, record)
            return 200, record
    raise APIError('Endpoint not found.', 404)

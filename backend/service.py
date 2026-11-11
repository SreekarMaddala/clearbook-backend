import copy
import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4

try:
    from .database import connection
except ImportError:
    from database import connection


class APIError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value, field, maximum=120, required=True):
    if not isinstance(value, str) or (required and not value.strip()) or len(value.strip()) > maximum:
        raise APIError(f'{field} must be {"a non-empty string" if required else "text"} of at most {maximum} characters.')
    return value.strip()


def paise(value):
    if isinstance(value, bool):
        raise APIError('Amount must be a positive number with at most two decimal places.')
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > Decimal('999999999999.99') or amount * 100 != (amount * 100).to_integral_value():
            raise InvalidOperation
        return int(amount * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise APIError('Amount must be positive, within range, and have at most two decimal places.')


def get_reference(db, table, key):
    if table not in ('accounts', 'vendors', 'categories', 'clients'):
        raise APIError('Unknown resource.', 404)
    if not isinstance(key, str) or not key.strip() or len(key) > 120:
        raise APIError(f'Choose a valid {table[:-1]}.')
    row = db.get(table, key)
    if not row:
        raise APIError(f'{table[:-1].capitalize()} not found.', 404)
    return row


PAYEE_ROLES = ('SUPPLIER', 'SUBCONTRACTOR', 'STAFF')
SALARY_CATEGORY = 'Staff Salaries'


def payee_roles(record):
    """Keep legacy payees readable until their explicit roles are migrated."""
    if 'roles' in record:
        return record['roles']
    category = record.get('category', '').casefold()
    if category in ('staff', 'staff salaries'):
        return ['STAFF']
    if 'sub contractor' in category or 'subcontractor' in category:
        return ['SUBCONTRACTOR']
    return ['SUPPLIER']


def reference(method, resource, record_id=None, body=None):
    with connection(write=method != 'GET') as db:
        if resource == 'locations':
            # Include historical free-text locations without rewriting transactions.
            names = {row['name'].casefold(): row['name'] for row in db.all('locations')}
            for row in db.all('transactions'):
                name = (row.get('location') or '').strip()
                if name:
                    names.setdefault(name.casefold(), name)
            if method == 'GET':
                return {'items': [{'name': name} for name in sorted(names.values(), key=str.casefold)]}
            name = text((body or {}).get('name'), 'Location name', 200)
            if name.casefold() in names:
                raise APIError('That location already exists.', 409)
            record = {'name': name}
            db.put('locations', str(uuid4()), record)
            return record
        if method == 'GET':
            rows = db.all(resource)
            if resource == 'vendors':
                rows = [{**r, 'roles': payee_roles(r)} for r in rows]
            return {'items': sorted(rows, key=lambda r: r['sort_order'] if resource == 'categories' else r['name'])}
        body = body or {}
        if resource == 'categories':
            name = text(body.get('name'), 'Category name', 80)
            rows = db.all('categories')
            if any(row['name'].casefold() == name.casefold() for row in rows):
                raise APIError('That category already exists.', 409)
            category = {'name': name, 'sort_order': max((row['sort_order'] for row in rows), default=-1) + 1}
            db.put('categories', name, category)
            return category
        existing = get_reference(db, resource, record_id) if record_id else {}
        data = {**existing, **body}
        name = text(data.get('name'), 'Name', 120)
        if resource == 'accounts':
            kind = data.get('type')
            if kind not in ('INTERNAL', 'THIRD_PARTY_CREDITOR'):
                raise APIError('Choose a valid account type.')
            if existing and kind != existing['type'] and any(t['payer'] == record_id or t.get('recipient') == record_id for t in db.all('transactions')):
                raise APIError('An account used in transactions cannot change ownership type.', 409)
            values = {'name': name, 'type': kind, 'owner': text(data.get('owner'), 'Owner'), 'detail': text(data.get('detail', ''), 'Description', 500, False)}
        elif resource == 'clients':
            email = text(data.get('email', ''), 'Email', 254, False)
            if email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
                raise APIError('Enter a valid email address.')
            values = {'name': name, 'contact': text(data.get('contact', ''), 'Contact person', 120, False),
                      'phone': text(data.get('phone', ''), 'Phone', 40, False), 'email': email}
        else:
            category = text(data.get('category'), 'Category', 80)
            get_reference(db, 'categories', category)
            roles = data.get('roles', payee_roles(data))
            if not isinstance(roles, list) or not roles or any(not isinstance(r, str) or r not in PAYEE_ROLES for r in roles):
                raise APIError('Choose Supplier, Subcontractor, or Staff roles.')
            roles = [r for r in PAYEE_ROLES if r in roles]
            if 'STAFF' not in roles and category == SALARY_CATEGORY:
                raise APIError('Staff Salaries requires the Staff role.')
            if existing and 'STAFF' in payee_roles(existing) and 'STAFF' not in roles and any(
                    t.get('vendor') == record_id and t.get('category') == SALARY_CATEGORY for t in db.all('transactions')):
                raise APIError('Staff with salary history must retain the Staff role.', 409)
            values = {'name': name, 'category': category, 'roles': roles,
                      'role': text(data.get('role', ''), 'Role', 500, False)}
        duplicate = next((r for r in db.all(resource) if r['name'].casefold() == name.casefold()), None)
        if duplicate and duplicate['id'] != record_id:
            raise APIError('That name already exists.', 409)
        record_id = record_id or str(uuid4())
        db.put(resource, record_id, {**existing, 'id': record_id, **values, 'created_at': existing.get('created_at', now())})
        return get_reference(db, resource, record_id)


def normalized_transaction(db, body):
    kind = body.get('type')
    if kind not in ('EXPENSE', 'PAYBACK', 'TRANSFER', 'CREDIT'):
        raise APIError('Choose Expense, Payback, or Credit.')
    day = body.get('date')
    try:
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError
    except (ValueError, TypeError):
        raise APIError('Payment date must be a valid YYYY-MM-DD date.')
    payer = None if kind == 'CREDIT' else get_reference(db, 'accounts', body.get('payer'))
    client_id = body.get('client_id') or None
    vendor = body.get('vendor') or None
    recipient = body.get('recipient') or None
    category = body.get('category') or None
    salary_month = body.get('salary_month') or None
    if salary_month is not None and (kind != 'EXPENSE' or category != SALARY_CATEGORY):
        raise APIError('Salary month is only used for Staff Salaries expenses.')
    if kind == 'CREDIT':
        if body.get('payer') or vendor or category:
            raise APIError('Credits use a client and receiving account, without a payer, vendor or expense category.')
        get_reference(db, 'clients', client_id)
        destination = get_reference(db, 'accounts', recipient)
        if destination['type'] != 'INTERNAL':
            raise APIError('Credits must be received into a business account.')
    elif client_id:
        raise APIError('Only credits can reference a client.')
    elif kind == 'EXPENSE':
        if recipient:
            raise APIError('Expenses do not use a destination account.')
        get_reference(db, 'categories', category)
        if vendor:
            get_reference(db, 'vendors', vendor)
        if category == SALARY_CATEGORY:
            employee = get_reference(db, 'vendors', vendor)
            if 'STAFF' not in payee_roles(employee):
                raise APIError('Select a staff member for salary payments.')
            try:
                if not isinstance(salary_month, str) or date.fromisoformat(salary_month + '-01').strftime('%Y-%m') != salary_month:
                    raise ValueError
            except (ValueError, TypeError):
                raise APIError('Choose the salary month in YYYY-MM format.')
    else:
        if vendor or category:
            raise APIError('Paybacks and transfers cannot have an expense category or vendor.')
        destination = get_reference(db, 'accounts', recipient)
        if payer['id'] == recipient:
            raise APIError('Source and destination accounts must differ.')
        if kind == 'PAYBACK' and (payer['type'] != 'INTERNAL' or destination['type'] != 'THIRD_PARTY_CREDITOR'):
            raise APIError('A payback must go from a business account to an external creditor.')
        if kind == 'TRANSFER' and destination['type'] != 'INTERNAL':
            raise APIError('Use Payback when sending money to an external creditor.')
    mode = body.get('mode')
    if mode not in ('UPI', 'NEFT', 'RTGS', 'IMPS', 'CASH', 'CHEQUE'):
        raise APIError('Invalid payment mode.')
    values = {'date': day, 'amount_paise': paise(body.get('amount')), 'type': kind,
            'payer': payer['id'] if payer else None, 'client_id': client_id, 'vendor': vendor, 'recipient': recipient, 'category': category,
            'mode': mode, 'location': text(body.get('location'), 'Location', 200),
            'notes': text(body.get('notes', ''), 'Notes', 1000, False),
            'receipt_name': text(body['receiptName'], 'Receipt filename', 255) if body.get('receiptName') else None}
    if category == SALARY_CATEGORY:
        values['salary_month'] = salary_month
    return values


def rebuild_allocations(db):
    """Rebuild FIFO matches atomically, with repayments limited to funding on/before their date."""
    db.replace('repayment_allocations', {})
    external = {a['id'] for a in db.all('accounts') if a['type'] == 'THIRD_PARTY_CREDITOR'}
    funding = {key: [] for key in external}
    active = sorted((t for t in db.all('transactions') if not t.get('reversed_at')), key=lambda t: (t['date'], t['type'] == 'PAYBACK', t['created_at'], t['id']))
    for row in active:
        if row['type'] in ('EXPENSE', 'TRANSFER') and row['payer'] in external:
            funding[row['payer']].append({'id': row['id'], 'remaining': row['amount_paise']})
        if row['type'] == 'PAYBACK':
            remaining = row['amount_paise']
            for source in funding[row['recipient']]:
                allocated = min(source['remaining'], remaining)
                if allocated:
                    db.put('repayment_allocations', row['id'] + '#' + source['id'], {'repayment_id': row['id'], 'funding_id': source['id'], 'amount_paise': allocated})
                    source['remaining'] -= allocated
                    remaining -= allocated
                if not remaining:
                    break
            if remaining:
                raise APIError('This change would overpay a creditor on a repayment date. Adjust the related repayments first.', 409)


def serialize_transactions(db, include_reversed=False):
    external = {a['id'] for a in db.all('accounts') if a['type'] == 'THIRD_PARTY_CREDITOR'}
    allocations = db.all('repayment_allocations')
    by_funding, by_repayment = {}, {}
    for allocation in allocations:
        by_funding[allocation['funding_id']] = by_funding.get(allocation['funding_id'], 0) + allocation['amount_paise']
        by_repayment.setdefault(allocation['repayment_id'], []).append({'funding_id': allocation['funding_id'], 'amount': allocation['amount_paise'] / 100})
    rows = sorted((t for t in db.all('transactions') if include_reversed or not t.get('reversed_at')), key=lambda t: (t['date'], t['created_at'], t['id']), reverse=True)
    result = []
    for row in rows:
        item = {k: v for k, v in row.items() if k not in ('amount_paise', 'history', 'idempotency_key', 'request_hash', 'receipt_name')}
        item['amount'] = row['amount_paise'] / 100
        item['receiptName'] = row['receipt_name']
        allocated = by_funding.get(row['id'], 0)
        if row['reversed_at']:
            status = ['reversed', 'Reversed']
        elif row['type'] == 'CREDIT':
            status = ['received', 'Received']
        elif row['type'] == 'PAYBACK':
            status = ['paid', 'Repaid']
        elif row['payer'] in external:
            status = ['paid', 'Settled'] if allocated == row['amount_paise'] else ['partial', 'Partially paid'] if allocated else ['pending', 'Pending payback']
        elif row['type'] == 'TRANSFER':
            status = ['transfer', 'Transfer']
        else:
            status = ['paid', 'Paid']
        item['status'] = status
        item['reimbursed_amount'] = allocated / 100
        item['allocations'] = by_repayment.get(row['id'], [])
        result.append(item)
    return result


def write_transaction(method, body, record_id=None, key=None, actor='local-admin'):
    with connection(write=True) as db:
        existing = copy.deepcopy(db.get('transactions', record_id)) if record_id else None
        if record_id:
            if not existing:
                raise APIError('Transaction not found.', 404)
            if method == 'REVERSE' and existing['reversed_at']:
                return next(t for t in serialize_transactions(db, True) if t['id'] == record_id)
            if existing['reversed_at']:
                raise APIError('A reversed transaction cannot be edited.', 409)
            if isinstance(body.get('version'), bool) or body.get('version') != existing['version']:
                raise APIError('This transaction changed. Refresh before editing it again.', 409)
        stamp = now()
        if method == 'REVERSE':
            reason = text(body.get('reason'), 'Reversal reason', 500)
            history = json.loads(existing['history']) + [{'action': 'reverse', 'actor': actor, 'at': stamp, 'before': {k:v for k,v in existing.items() if k != 'history'}}]
            db.put('transactions', record_id, {**existing, 'reversed_at': stamp, 'reversal_reason': reason,
                   'version': existing['version'] + 1, 'updated_at': stamp, 'history': json.dumps(history)})
        else:
            merged = {**existing, 'amount': str(Decimal(existing['amount_paise']) / 100), 'receiptName': existing['receipt_name'], **body} if existing else body
            if existing and merged.get('category') != SALARY_CATEGORY and 'salary_month' not in body:
                merged.pop('salary_month', None)
            values = normalized_transaction(db, merged)
            # Preserve hashes for retries of transactions created before clients existed.
            hash_values = {k:v for k,v in values.items() if k != 'client_id' or v is not None}
            digest = hashlib.sha256(json.dumps(hash_values, sort_keys=True).encode()).hexdigest()
            if not record_id:
                key = text(key, 'Idempotency-Key header', 128)
                duplicate = next((t for t in db.all('transactions') if t.get('idempotency_key') == key), None)
                if duplicate:
                    if duplicate['request_hash'] != digest:
                        raise APIError('This idempotency key was already used for different transaction data.', 409)
                    return next(t for t in serialize_transactions(db, True) if t['id'] == duplicate['id'])
            if existing:
                history = json.loads(existing['history']) + [{'action': 'update', 'actor': actor, 'at': stamp, 'before': {k:v for k,v in existing.items() if k != 'history'}}]
                values = {**existing, **values, 'updated_at': stamp, 'version': existing['version'] + 1, 'history': json.dumps(history)}
                if values['category'] != SALARY_CATEGORY:
                    values.pop('salary_month', None)
            else:
                record_id = str(uuid4())
                values.update(id=record_id, created_at=stamp, updated_at=stamp, idempotency_key=key, request_hash=digest,
                              version=1, reversed_at=None, reversal_reason=None,
                              history=json.dumps([{'action': 'create', 'actor': actor, 'at': stamp}]))
            db.put('transactions', record_id, values)
        rebuild_allocations(db)
        return next(t for t in serialize_transactions(db, True) if t['id'] == record_id)


def transaction_list(query):
    try:
        page, limit = int(query.get('page', 1)), int(query.get('limit', 100))
        if page < 1 or not 1 <= limit <= 200:
            raise ValueError
    except (ValueError, TypeError):
        raise APIError('Page must be positive and limit must be between 1 and 200.')
    with connection() as db:
        rows = serialize_transactions(db, query.get('include_reversed') == 'true')
        names = {a['id']:a['name'] for a in db.all('accounts')}
        vendors = {a['id']:a['name'] for a in db.all('vendors')}
        clients = {a['id']:a['name'] for a in db.all('clients')}
        for field in ('payer', 'recipient', 'client_id', 'vendor', 'category', 'location', 'type', 'salary_month'):
            if query.get(field):
                rows = [t for t in rows if t.get(field) == query[field]]
        if query.get('account'):
            rows = [t for t in rows if query['account'] in (t.get('payer'), t.get('recipient'))]
        if query.get('from'):
            rows = [t for t in rows if t['date'] >= query['from']]
        if query.get('to'):
            rows = [t for t in rows if t['date'] <= query['to']]
        if query.get('status'):
            rows = [t for t in rows if t['status'][0] == query['status']]
        if query.get('q'):
            needle = query['q'].lower()
            rows = [t for t in rows if needle in ' '.join(str(x or '') for x in (t['location'], t['category'], t['mode'], t['amount'], names.get(t['payer']), names.get(t['recipient']), vendors.get(t['vendor']), clients.get(t.get('client_id')))).lower()]
        return {'items':rows[(page-1)*limit:page*limit], 'total':len(rows), 'page':page, 'limit':limit}


def dashboard():
    with connection() as db:
        rows = [t for t in db.all('transactions') if not t.get('reversed_at')]
        creditors = []
        for account in sorted((a for a in db.all('accounts') if a['type'] == 'THIRD_PARTY_CREDITOR'), key=lambda a: a['name']):
            funded = sum(t['amount_paise'] for t in rows if t['payer'] == account['id'] and t['type'] in ('EXPENSE','TRANSFER'))
            repaid = sum(t['amount_paise'] for t in rows if t['recipient'] == account['id'] and t['type'] == 'PAYBACK')
            creditors.append({'id':account['id'], 'fronted':funded/100, 'settled':repaid/100, 'balance':(funded-repaid)/100})
        creditor_ids = {c['id'] for c in creditors}
        funded_paise = sum(t['amount_paise'] for t in rows if t['payer'] in creditor_ids and t['type'] in ('EXPENSE','TRANSFER'))
        repaid_paise = sum(t['amount_paise'] for t in rows if t['type'] == 'PAYBACK')
        amounts = {'expenses':sum(t['amount_paise'] for t in rows if t['type']=='EXPENSE')/100,
                   'fronted':funded_paise/100, 'settled':repaid_paise/100,
                   'outstanding':(funded_paise-repaid_paise)/100,
                   'credits':sum(t['amount_paise'] for t in rows if t['type']=='CREDIT')/100}
        accounts = []
        for account in db.all('accounts'):
            if account['type'] != 'INTERNAL':
                continue
            incoming = sum(t['amount_paise'] for t in rows if t.get('recipient') == account['id'] and t['type'] in ('CREDIT','TRANSFER'))
            outgoing = sum(t['amount_paise'] for t in rows if t.get('payer') == account['id'])
            credits = sum(t['amount_paise'] for t in rows if t.get('recipient') == account['id'] and t['type']=='CREDIT')
            accounts.append({'id':account['id'], 'incoming':incoming/100, 'credits':credits/100,
                             'outgoing':outgoing/100, 'net_movement':(incoming-outgoing)/100})
        clients = [{'id':c['id'], 'received':sum(t['amount_paise'] for t in rows if t['type']=='CREDIT' and t.get('client_id')==c['id'])/100}
                   for c in db.all('clients')]
        return {'totals':amounts, 'creditors':creditors, 'accounts':accounts, 'clients':clients, 'generated_at':now()}

import copy
import hashlib
import json
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
    if table not in ('accounts', 'vendors', 'categories'):
        raise APIError('Unknown resource.', 404)
    if not isinstance(key, str) or not key.strip() or len(key) > 120:
        raise APIError(f'Choose a valid {table[:-1]}.')
    row = db.get(table, key)
    if not row:
        raise APIError(f'{table[:-1].capitalize()} not found.', 404)
    return row


def reference(method, resource, record_id=None, body=None):
    with connection(write=method != 'GET') as db:
        if method == 'GET':
            return {'items': sorted(db.all(resource), key=lambda r: r['sort_order'] if resource == 'categories' else r['name'])}
        body = body or {}
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
        else:
            category = text(data.get('category'), 'Category', 80)
            get_reference(db, 'categories', category)
            values = {'name': name, 'category': category, 'role': text(data.get('role', ''), 'Role', 500, False)}
        duplicate = next((r for r in db.all(resource) if r['name'].casefold() == name.casefold()), None)
        if duplicate and duplicate['id'] != record_id:
            raise APIError('That name already exists.', 409)
        record_id = record_id or str(uuid4())
        db.put(resource, record_id, {**existing, 'id': record_id, **values, 'created_at': existing.get('created_at', now())})
        return get_reference(db, resource, record_id)


def normalized_transaction(db, body):
    kind = body.get('type')
    if kind not in ('EXPENSE', 'PAYBACK', 'TRANSFER'):
        raise APIError('Choose Expense, Payback, or Transfer.')
    day = body.get('date')
    try:
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError
    except (ValueError, TypeError):
        raise APIError('Payment date must be a valid YYYY-MM-DD date.')
    payer = get_reference(db, 'accounts', body.get('payer'))
    vendor = body.get('vendor') or None
    recipient = body.get('recipient') or None
    category = body.get('category') or None
    if kind == 'EXPENSE':
        if recipient:
            raise APIError('Expenses do not use a destination account.')
        get_reference(db, 'categories', category)
        if vendor:
            get_reference(db, 'vendors', vendor)
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
    return {'date': day, 'amount_paise': paise(body.get('amount')), 'type': kind,
            'payer': payer['id'], 'vendor': vendor, 'recipient': recipient, 'category': category,
            'mode': mode, 'location': text(body.get('location'), 'Location', 200),
            'notes': text(body.get('notes', ''), 'Notes', 1000, False),
            'receipt_name': text(body['receiptName'], 'Receipt filename', 255) if body.get('receiptName') else None}


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
            values = normalized_transaction(db, merged)
            digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
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
        for field in ('payer', 'vendor', 'category', 'location', 'type'):
            if query.get(field):
                rows = [t for t in rows if t.get(field) == query[field]]
        if query.get('from'):
            rows = [t for t in rows if t['date'] >= query['from']]
        if query.get('to'):
            rows = [t for t in rows if t['date'] <= query['to']]
        if query.get('status'):
            rows = [t for t in rows if t['status'][0] == query['status']]
        if query.get('q'):
            needle = query['q'].lower()
            rows = [t for t in rows if needle in ' '.join(str(x or '') for x in (t['notes'], t['location'], t['category'], t['mode'], t['amount'], names.get(t['payer']), names.get(t['recipient']), vendors.get(t['vendor']))).lower()]
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
                   'outstanding':(funded_paise-repaid_paise)/100}
        return {'totals':amounts, 'creditors':creditors, 'generated_at':now()}

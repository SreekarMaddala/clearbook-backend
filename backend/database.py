"""DynamoDB entity document storage; local JSON adapter for offline development."""
import copy
import json
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
CATEGORIES = ['Fuel', 'Food', 'Porter', 'Labour Transport', 'Power Tools', 'Toll',
              'Machinery Repair', 'Machinery Purchase', 'Other Expense', 'Labour Tips', 'Sub Contractor', 'Staff Salaries']
ENTITIES = ('accounts', 'vendors', 'categories', 'transactions', 'repayment_allocations', 'clients', 'locations')
REGISTRY_ENTITIES = ('organizations', 'users')
_LOCAL_LOCK = threading.RLock()
_WORKSPACE = ContextVar('clearbook_workspace', default=None)
PLATFORM_WORKSPACE = '__provider__'


@contextmanager
def workspace_scope(workspace_id):
    """Select storage using server-authorized identity, never client input."""
    token = _WORKSPACE.set(workspace_id)
    try:
        yield
    finally:
        _WORKSPACE.reset(token)


class StoreConflict(Exception):
    pass


class StoreLimitError(Exception):
    pass


def empty_state(entities=ENTITIES):
    return {name: {} for name in entities}


class Documents:
    def __init__(self, state):
        self.state = state

    def all(self, entity):
        return list(self.state[entity].values())

    def get(self, entity, key):
        return self.state[entity].get(key)

    def put(self, entity, key, value):
        self.state[entity][key] = value

    def replace(self, entity, values):
        self.state[entity] = values


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


class DynamoStore:
    def __init__(self, table, client=None, entities=ENTITIES):
        import boto3
        from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
        self.client = client or boto3.client('dynamodb', region_name=os.environ.get('AWS_REGION', 'ap-south-2'))
        self.entities = tuple(entities)
        self.tables = {entity: table[entity] for entity in self.entities if entity in table} if isinstance(table, dict) else {entity: table for entity in self.entities}
        # Locations share the categories table with a separate entity/SK prefix.
        # Existing six-table deployments need no new table or environment setting.
        if 'locations' in self.entities and 'locations' not in self.tables and 'categories' in self.tables:
            self.tables['locations'] = self.tables['categories']
        if set(self.tables) != set(self.entities):
            raise ValueError('Required entity table mappings are missing: '+', '.join(set(self.entities)-set(self.tables)))
        self.table = self.tables['organizations' if self.entities == REGISTRY_ENTITIES else 'accounts']
        self.legacy = len(set(self.tables.values())) == 1
        self.pk = 'WORKSPACE#' + (_WORKSPACE.get() or os.environ.get('WORKSPACE_ID', 'clearbook'))
        self.serializer, self.deserializer = TypeSerializer(), TypeDeserializer()

    def encode(self, value):
        return {k: self.serializer.serialize(v) for k, v in value.items()}

    def decode(self, value):
        return _plain({k: self.deserializer.deserialize(v) for k, v in value.items()})

    def revision(self):
        item = self.client.get_item(TableName=self.table, Key=self.encode({'PK': self.pk, 'SK': 'META'}), ConsistentRead=True).get('Item')
        return self.decode(item)['revision'] if item else 0

    def read(self):
        # Strongly consistent reads bracketed by the revision detect cross-page changes.
        for _ in range(3):
            revision = self.revision()
            state = empty_state(self.entities)
            for table in dict.fromkeys(self.tables.values()):
                cursor = None
                while True:
                    query = {'TableName': table, 'KeyConditionExpression': 'PK = :pk',
                             'ExpressionAttributeValues': {':pk': {'S': self.pk}}, 'ConsistentRead': True}
                    if cursor:
                        query['ExclusiveStartKey'] = cursor
                    response = self.client.query(**query)
                    for raw in response.get('Items', []):
                        item = self.decode(raw)
                        entity = item.get('entity')
                        if entity in self.entities:
                            if not self.legacy and self.tables[entity] != table:
                                raise StoreConflict('Storage migration is required. Run scripts/migrate-five-tables.py before using this backend.')
                            state[entity][item['record_id']] = item['data']
                    cursor = response.get('LastEvaluatedKey')
                    if not cursor:
                        break
            if revision == self.revision():
                return revision, state
        raise StoreConflict('The ledger changed while loading. Please refresh.')

    def commit(self, revision, before, after):
        actions = []
        for entity in self.entities:
            for key in sorted(before[entity].keys() | after[entity].keys()):
                old, new = before[entity].get(key), after[entity].get(key)
                if old == new:
                    continue
                item_key = {'PK': self.pk, 'SK': entity + '#' + key}
                if new is None:
                    actions.append({'Delete': {'TableName': self.tables[entity], 'Key': self.encode(item_key)}})
                else:
                    item = {**item_key, 'entity': entity, 'record_id': key, 'data': new}
                    if len(json.dumps(item, ensure_ascii=False).encode('utf-8')) > 300000:
                        raise StoreLimitError('This record has reached its audit-history size limit. Contact the administrator.')
                    actions.append({'Put': {'TableName': self.tables[entity], 'Item': self.encode(item)}})
        if not actions:
            return
        if len(actions) > 99:
            raise StoreLimitError('This change affects too many allocation records for one atomic write. Split the repayment or adjust fewer entries.')
        meta = {'TableName': self.table, 'Item': self.encode({'PK': self.pk, 'SK': 'META', 'revision': revision + 1})}
        if revision:
            meta.update(ConditionExpression='#revision = :expected', ExpressionAttributeNames={'#revision': 'revision'},
                        ExpressionAttributeValues={':expected': {'N': str(revision)}})
        else:
            meta['ConditionExpression'] = 'attribute_not_exists(PK)'
        actions.append({'Put': meta})
        if len(json.dumps(actions).encode('utf-8')) > 3500000:
            raise StoreLimitError('This update is too large for one atomic write. Split it into smaller changes.')
        try:
            self.client.transact_write_items(TransactItems=actions, ClientRequestToken=str(uuid4()))
        except self.client.exceptions.TransactionCanceledException as error:
            reasons = error.response.get('CancellationReasons', [])
            if any(r.get('Code') in ('ConditionalCheckFailed', 'TransactionConflict') for r in reasons):
                raise StoreConflict('Another user changed the ledger. Refresh and retry.') from error
            raise


@contextmanager
def _local_state(write):
    path = Path(os.environ.get('LOCAL_DATA_PATH', str(ROOT.parent / '.local' / 'clearbook.json')))
    workspace = _WORKSPACE.get()
    if workspace and workspace != os.environ.get('WORKSPACE_ID', 'clearbook'):
        # Hash the server-selected ID so it cannot become a filesystem path.
        import hashlib
        suffix = hashlib.sha256(workspace.encode()).hexdigest()
        path = path.with_name(f'{path.stem}.{suffix}{path.suffix}')
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCAL_LOCK, open(str(path) + '.lock', 'a+b') as lock:
        lock.seek(0, 2)
        if not lock.tell():
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else empty_state()
            for entity in ENTITIES:
                state.setdefault(entity, {})
            if workspace == PLATFORM_WORKSPACE:
                for entity in REGISTRY_ENTITIES:
                    state.setdefault(entity, {})
                # Upgrade local provider documents without touching business accounts.
                for key, record in list(state['accounts'].items()):
                    entity = {'customer':'organizations','user':'users'}.get(record.get('kind'))
                    if entity:
                        state[entity].setdefault(key,record)
                        del state['accounts'][key]
            original = copy.deepcopy(state)
            yield Documents(state)
            if write and state != original:
                temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
                with temporary.open('w', encoding='utf-8') as output:
                    json.dump(state, output, ensure_ascii=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def connection(write=False):
    table = json.loads(os.environ['DYNAMODB_TABLES']) if os.environ.get('DYNAMODB_TABLES') else os.environ.get('DYNAMODB_TABLE')
    if table:
        store = DynamoStore(table, entities=REGISTRY_ENTITIES if _WORKSPACE.get() == PLATFORM_WORKSPACE else ENTITIES)
        revision, state = store.read()
        original = copy.deepcopy(state)
        yield Documents(state)
        if write:
            store.commit(revision, original, state)
    else:
        if os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):
            raise RuntimeError('DYNAMODB_TABLES is required in Lambda. Local documents are development-only.')
        with _local_state(write) as documents:
            yield documents


def initialize():
    with connection(write=True) as db:
        for i, name in enumerate(CATEGORIES):
            if not db.get('categories', name):
                db.put('categories', name, {'name': name, 'sort_order': i})


if __name__ == '__main__':
    initialize()
    print(f'Document store initialized with {len(CATEGORIES)} categories.')

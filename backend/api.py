"""The 12-route contract, shared by the local HTTP server and Lambda handlers."""
import base64
import json
import logging
import re

try:
    from .access import identity
    from .database import workspace_scope
    from .provider import dispatch_provider
    from .database import StoreConflict, StoreLimitError
    from .service import APIError, dashboard, reference, transaction_list, write_transaction
except ImportError:
    from access import identity
    from database import workspace_scope
    from provider import dispatch_provider
    from database import StoreConflict, StoreLimitError
    from service import APIError, dashboard, reference, transaction_list, write_transaction

ROUTES = [
    ('GET', r'/accounts', 'reference'), ('POST', r'/accounts', 'reference'), ('PATCH', r'/accounts/([^/]+)', 'reference'),
    ('GET', r'/vendors', 'reference'), ('POST', r'/vendors', 'reference'), ('PATCH', r'/vendors/([^/]+)', 'reference'),
    ('GET', r'/categories', 'reference'),
    ('GET', r'/transactions', 'transactions'), ('POST', r'/transactions', 'transactions'),
    ('PATCH', r'/transactions/([^/]+)', 'transactions'), ('POST', r'/transactions/([^/]+)/reverse', 'transactions'),
    ('GET', r'/dashboard', 'dashboard'),
]


def dispatch(method, path, body=None, query=None, headers=None, group=None, actor='local-admin'):
    try:
        path = path.removeprefix('/api').rstrip('/') or '/'
        match = None
        for verb, pattern, owner in ROUTES:
            if verb == method:
                route_match = re.fullmatch(pattern, path)
                if route_match:
                    match = route_match, owner
                    break
        if not match or (group and group != match[1]):
            raise APIError('Endpoint not found.', 404)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise APIError('Request body must be a JSON object.')
        headers = {k.lower():v for k,v in (headers or {}).items()}
        record_id = match[0].group(1) if match[0].groups() else None
        if match[1] == 'reference':
            result = reference(method, path.split('/')[1], record_id, body)
        elif match[1] == 'dashboard':
            result = dashboard()
        elif method == 'GET':
            result = transaction_list(query or {})
        else:
            result = write_transaction('REVERSE' if path.endswith('/reverse') else method, body, record_id, headers.get('idempotency-key'), actor)
        return (201 if method == 'POST' and not path.endswith('/reverse') else 200), result
    except (StoreConflict, StoreLimitError) as error:
        return 409, {'error': str(error)}
    except APIError as error:
        return error.status, {'error':str(error)}
    except Exception:
        logging.exception('API request failed: %s %s', method, path)
        return 500, {'error':'The server could not complete this request. Please retry or refresh.'}


def lambda_response(event, group):
    try:
        payload = event.get('body') or '{}'
        if event.get('isBase64Encoded'):
            payload = base64.b64decode(payload).decode('utf-8')
        if len(payload.encode('utf-8')) > 1024 * 1024:
            raise APIError('Request body exceeds 1 MB.', 413)
        body = json.loads(payload)
        method = event.get('requestContext', {}).get('http', {}).get('method', event.get('httpMethod', 'GET'))
        claims = event.get('requestContext', {}).get('authorizer', {}).get('jwt', {}).get('claims', {})
        # API Gateway validates the signature, issuer, audience, expiry and scope.
        # Fail closed if a handler is invoked without its authenticated API context.
        if not claims.get('sub') or claims.get('token_use') != 'access':
            raise APIError('A valid Cognito access token is required.', 401)
        if 'clearbook/ledger' not in str(claims.get('scope', '')).split():
            raise APIError('This token does not permit workspace access.', 403)
        if not isinstance(body, dict):
            raise APIError('Request body must be a JSON object.')
        path = event.get('rawPath', event.get('path', '/')).removeprefix('/api').rstrip('/') or '/'
        session = identity(claims)
        if path == '/session' and method == 'GET' and group == 'reference':
            status, result = 200, session
        elif path.startswith('/provider/'):
            if session['role'] != 'provider':
                raise APIError('Software-provider access is required.', 403)
            if group != 'reference':
                raise APIError('Endpoint not found.', 404)
            status, result = dispatch_provider(method, path, body)
        else:
            if session['role'] == 'provider':
                raise APIError('Software-provider accounts cannot access customer ledgers.', 403)
            with workspace_scope(session['workspace']['id']):
                status, result = dispatch(method, path, body,
                                          event.get('queryStringParameters') or {}, event.get('headers') or {}, group, claims['sub'])
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        status, result = 400, {'error':'Invalid JSON request body.'}
    except APIError as error:
        status, result = error.status, {'error':str(error)}
    except (StoreConflict, StoreLimitError) as error:
        status, result = 409, {'error': str(error)}
    except Exception:
        logging.exception('Authenticated API request failed.')
        status, result = 500, {'error': 'The server could not complete this request. Please retry or refresh.'}
    return {'statusCode':status, 'headers':{'Content-Type':'application/json; charset=utf-8', 'Cache-Control':'no-store'}, 'body':json.dumps(result)}

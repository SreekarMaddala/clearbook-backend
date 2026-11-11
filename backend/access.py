"""Resolve provider accounts and customer workspace membership."""
import json

try:
    from .database import PLATFORM_WORKSPACE, connection, workspace_scope
    from .service import APIError
except ImportError:
    from database import PLATFORM_WORKSPACE, connection, workspace_scope
    from service import APIError


def is_provider(claims):
    groups = claims.get('cognito:groups', [])
    if isinstance(groups, str):
        try:
            groups = json.loads(groups)
        except ValueError:
            groups = groups.strip('[]').split(',')
    return isinstance(groups, list) and 'SUPER_ADMIN' in groups


def identity(claims):
    if is_provider(claims):
        return {'role': 'provider', 'name': 'Software provider', 'workspace': None}
    with workspace_scope(PLATFORM_WORKSPACE), connection() as db:
        member = db.get('users', 'user#' + claims['sub'])
        if not member or not member.get('enabled') or member.get('deleted_at'):
            raise APIError('Your account has no active organization access. Contact your software provider.', 403)
        organization = db.get('organizations', 'customer#' + member['customer_id'])
        if not organization or not organization.get('enabled') or organization.get('deleted_at'):
            raise APIError('Your organization is disabled. Contact your software provider.', 403)
        return {'role': 'customer', 'name': member['email'],
                'workspace': {'id': organization['id'], 'name': organization['name']}}

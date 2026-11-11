try:
    from .api import lambda_response
except ImportError:
    from api import lambda_response


def handler(event, context):
    # Administrative IAM-only invocation for the private database; not an HTTP route.
    if event == {'operation': 'initialize_database'}:
        try:
            from .database import initialize, CATEGORIES
        except ImportError:
            from database import initialize, CATEGORIES
        initialize()
        return {'initialized': True, 'categories': len(CATEGORIES)}
    return lambda_response(event, 'reference')

try:
    from .api import lambda_response
except ImportError:
    from api import lambda_response


def handler(event, context):
    # Administrative IAM-only invocation for the private database; not an HTTP route.
    if event == {'operation': 'initialize_database'}:
        try:
            from .database import initialize
        except ImportError:
            from database import initialize
        initialize()
        return {'initialized': True, 'categories': 11}
    return lambda_response(event, 'reference')

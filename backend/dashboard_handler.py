try:
    from .api import lambda_response
except ImportError:
    from api import lambda_response


def handler(event, context):
    return lambda_response(event, 'dashboard')

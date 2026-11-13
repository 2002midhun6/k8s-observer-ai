import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import aws
from .agent import chat


def _status():
    try:
        identity = aws.caller_identity()
        return {'connected': True, 'arn': identity['Arn'], 'account': identity['Account']}
    except Exception as e:
        return {'connected': False, 'error': f'{type(e).__name__}: {e}'}


def _base():
    return {'profile': aws.PROFILE, 'region': aws.REGION, 'cluster': aws.CLUSTER}


@require_GET
def aws_status(request):
    return JsonResponse({**_base(), **_status()})


@csrf_exempt
@require_POST
def aws_connect(request):
    """Opens the AWS sign-in page in a browser tab and waits for the login to finish."""
    try:
        aws.run_aws_login()
    except Exception as e:
        return JsonResponse({**_base(), 'connected': False, 'error': str(e)}, status=400)
    return JsonResponse({**_base(), **_status()})


@csrf_exempt
@require_POST
def chat_view(request):
    try:
        messages = json.loads(request.body)['messages']
    except (ValueError, KeyError):
        return JsonResponse({'error': 'Body must be {"messages": [...]}'}, status=400)
    status = _status()
    if not status['connected']:
        return JsonResponse({'error': 'Not connected to AWS. Click "Connect to AWS" first.'}, status=401)
    try:
        reply, tool_calls = chat(messages)
    except Exception as e:
        return JsonResponse({'error': f'{type(e).__name__}: {e}'}, status=500)
    return JsonResponse({'reply': reply, 'tool_calls': tool_calls})

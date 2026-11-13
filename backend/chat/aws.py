"""
Connection to AWS and the EKS cluster, with read-only guards.

Credentials come from `aws login` (browser sign-in), stored by the AWS CLI
under the profile AWS_AGENT_PROFILE. boto3 reads them directly; the Kubernetes
API is reached with a short-lived EKS token signed from the same credentials.
"""
import base64
import os
import subprocess
import threading
import time
from pathlib import Path

import boto3
import yaml
from botocore.signers import RequestSigner
from kubernetes import client as k8s

PROFILE = os.getenv('AWS_AGENT_PROFILE', 'ai-agent')
REGION = os.getenv('AWS_REGION', 'ap-south-1')
CLUSTER = os.getenv('EKS_CLUSTER_NAME', 'demo-eks')

# Every AWS API operation the app is allowed to call. Anything else is
# rejected before the request is signed or sent.
ALLOWED_AWS_OPERATIONS = {
    'GetCallerIdentity',
    'DescribeCluster', 'ListNodegroups', 'DescribeNodegroup',
    'DescribeVpcs', 'DescribeSubnets', 'DescribeRouteTables',
    'DescribeInternetGateways', 'DescribeNatGateways', 'DescribeSecurityGroups',
    'DescribeLoadBalancers', 'DescribeListeners', 'DescribeTargetGroups',
    'DescribeTargetHealth', 'DescribeInstanceHealth',
}


class ReadOnlyViolation(Exception):
    pass


def _guard_aws_call(model, **kwargs):
    if model.name not in ALLOWED_AWS_OPERATIONS:
        raise ReadOnlyViolation(
            f'Blocked AWS call {model.service_model.service_name}:{model.name} (app is read-only)'
        )


def get_session():
    """A boto3 session using the `aws login` profile, with the write guard attached."""
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    session.events.register('before-call', _guard_aws_call)
    return session


def caller_identity():
    return get_session().client('sts').get_caller_identity()


_login_lock = threading.Lock()


def run_aws_login(timeout=300):
    """Run `aws login`, which opens the AWS sign-in page in a browser tab."""
    if not _login_lock.acquire(blocking=False):
        raise RuntimeError('A sign-in is already in progress. Finish it in the browser tab.')
    try:
        result = subprocess.run(
            ['aws', 'login', '--profile', PROFILE, '--region', REGION],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or 'aws login failed')
    finally:
        _login_lock.release()


# --- Kubernetes -------------------------------------------------------------

class ReadOnlyApiClient(k8s.ApiClient):
    """Kubernetes API client that only allows GET requests."""

    def call_api(self, method, url, *args, **kwargs):
        if method.upper() != 'GET':
            raise ReadOnlyViolation(f'Blocked Kubernetes {method} {url} (app is read-only)')
        return super().call_api(method, url, *args, **kwargs)


_cluster_cache = {'at': 0, 'endpoint': None, 'ca': None, 'info': None}


def cluster_info(session):
    """EKS DescribeCluster result (cached 5 min); None if not permitted."""
    if time.time() - _cluster_cache['at'] < 300 and _cluster_cache['endpoint']:
        return _cluster_cache['info']
    info = None
    try:
        info = session.client('eks').describe_cluster(name=CLUSTER)['cluster']
        endpoint, ca = info['endpoint'], info['certificateAuthority']['data']
    except Exception:
        endpoint, ca = _endpoint_from_kubeconfig()
    _cluster_cache.update(at=time.time(), endpoint=endpoint, ca=ca, info=info)
    return info


def _endpoint_from_kubeconfig():
    """Fallback when eks:DescribeCluster is denied: read endpoint + CA from ~/.kube/config."""
    path = Path(os.getenv('KUBECONFIG', Path.home() / '.kube' / 'config'))
    kubeconfig = yaml.safe_load(path.read_text())
    for entry in kubeconfig.get('clusters', []):
        if entry['name'] == CLUSTER or entry['name'].endswith(f'/{CLUSTER}'):
            return entry['cluster']['server'], entry['cluster']['certificate-authority-data']
    raise RuntimeError(f'Cannot find endpoint for cluster {CLUSTER}: DescribeCluster denied and not in kubeconfig')


def _eks_token(session):
    """Same token `aws eks get-token` produces: a presigned STS GetCallerIdentity URL."""
    sts = session.client('sts')
    signer = RequestSigner(
        sts.meta.service_model.service_id, REGION, 'sts', 'v4',
        session.get_credentials(), session.events,
    )
    url = signer.generate_presigned_url(
        {
            'method': 'GET',
            'url': f'https://sts.{REGION}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15',
            'body': {},
            'headers': {'x-k8s-aws-id': CLUSTER},
            'context': {},
        },
        region_name=REGION, expires_in=60, operation_name='',
    )
    return 'k8s-aws-v1.' + base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')


def k8s_client(session):
    cluster_info(session)
    cfg = k8s.Configuration(
        host=_cluster_cache['endpoint'],
        ca_cert_data=base64.b64decode(_cluster_cache['ca']).decode(),
        api_key={'BearerToken': _eks_token(session)},
        api_key_prefix={'BearerToken': 'Bearer'},
    )
    return ReadOnlyApiClient(cfg)

"""
Read-only tools the AI agent can call. Each returns plain JSON-serialisable data.
"""
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from kubernetes import client as k8s
from kubernetes.client.exceptions import ApiException
from kubernetes.utils import parse_quantity

from . import aws


class Context:
    """Lazily created AWS session and Kubernetes client for one chat request."""

    def __init__(self):
        self.session = aws.get_session()
        self._api = None

    @property
    def api(self):
        if self._api is None:
            self._api = aws.k8s_client(self.session)
        return self._api


def _age(ts):
    if not ts:
        return None
    secs = int((datetime.now(timezone.utc) - ts).total_seconds())
    for unit, size in (('d', 86400), ('h', 3600), ('m', 60)):
        if secs >= size:
            return f'{secs // size}{unit}'
    return f'{secs}s'


# --- Kubernetes ------------------------------------------------------------

def list_pods(ctx, namespace=None):
    core = k8s.CoreV1Api(ctx.api)
    pods = core.list_namespaced_pod(namespace).items if namespace else core.list_pod_for_all_namespaces().items
    rows, summary = [], {}
    for p in pods:
        statuses = p.status.container_statuses or []
        ready = sum(1 for c in statuses if c.ready)
        waiting = [c.state.waiting.reason for c in statuses if c.state and c.state.waiting]
        rows.append({
            'namespace': p.metadata.namespace,
            'name': p.metadata.name,
            'phase': p.status.phase,
            'ready': f'{ready}/{len(p.spec.containers)}',
            'restarts': sum(c.restart_count for c in statuses),
            'waiting_reasons': waiting or None,
            'node': p.spec.node_name,
            'pod_ip': p.status.pod_ip,
            'age': _age(p.metadata.creation_timestamp),
        })
        group = 'system (kube-system)' if p.metadata.namespace == 'kube-system' else 'application'
        summary.setdefault(group, {}).setdefault(p.status.phase, 0)
        summary[group][p.status.phase] += 1
    return {'summary_by_phase': summary, 'total': len(rows), 'pods': rows}


def get_deployments(ctx, namespace=None):
    apps = k8s.AppsV1Api(ctx.api)
    deps = apps.list_namespaced_deployment(namespace).items if namespace else apps.list_deployment_for_all_namespaces().items
    autoscaling = k8s.AutoscalingV2Api(ctx.api)
    hpas = autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces().items
    rows = []
    for d in deps:
        annotations = d.metadata.annotations or {}
        labels = d.metadata.labels or {}
        hpa = next((h for h in hpas if h.metadata.namespace == d.metadata.namespace
                    and h.spec.scale_target_ref.name == d.metadata.name), None)
        rows.append({
            'namespace': d.metadata.namespace,
            'name': d.metadata.name,
            'replicas_desired': d.spec.replicas,
            'replicas_ready': d.status.ready_replicas or 0,
            'replicas_available': d.status.available_replicas or 0,
            'replicas_updated': d.status.updated_replicas or 0,
            'images': [c.image for c in d.spec.template.spec.containers],
            'resources': [{'container': c.name, 'requests': c.resources.requests, 'limits': c.resources.limits}
                          for c in d.spec.template.spec.containers],
            'selector': d.spec.selector.match_labels,
            'strategy': d.spec.strategy.type,
            'managed_by': ('helm' if labels.get('app.kubernetes.io/managed-by') == 'Helm'
                           else 'kubectl apply' if 'kubectl.kubernetes.io/last-applied-configuration' in annotations
                           else labels.get('app.kubernetes.io/managed-by', 'unknown')),
            'autoscaler': {'name': hpa.metadata.name, 'min': hpa.spec.min_replicas, 'max': hpa.spec.max_replicas}
                          if hpa else None,
            'conditions': [{'type': c.type, 'status': c.status, 'reason': c.reason} for c in d.status.conditions or []],
            'age': _age(d.metadata.creation_timestamp),
        })
    return {'deployments': rows}


def get_services(ctx, namespace=None):
    core = k8s.CoreV1Api(ctx.api)
    discovery = k8s.DiscoveryV1Api(ctx.api)
    svcs = core.list_namespaced_service(namespace).items if namespace else core.list_service_for_all_namespaces().items
    rows = []
    for s in svcs:
        slices = discovery.list_namespaced_endpoint_slice(
            s.metadata.namespace, label_selector=f'kubernetes.io/service-name={s.metadata.name}').items
        ready_ips = [ip for sl in slices for ep in sl.endpoints or []
                     if ep.conditions and ep.conditions.ready for ip in ep.addresses]
        ingress = (s.status.load_balancer.ingress or []) if s.status.load_balancer else []
        rows.append({
            'namespace': s.metadata.namespace,
            'name': s.metadata.name,
            'type': s.spec.type,
            'cluster_ip': s.spec.cluster_ip,
            'ports': [{'port': p.port, 'target_port': str(p.target_port), 'node_port': p.node_port,
                       'protocol': p.protocol} for p in s.spec.ports or []],
            'selector': s.spec.selector,
            'load_balancer_hostnames': [i.hostname or i.ip for i in ingress] or None,
            'annotations': {k: v for k, v in (s.metadata.annotations or {}).items()
                            if 'load-balancer' in k or 'aws' in k} or None,
            'ready_endpoint_ips': ready_ips,
        })
    return {'services': rows}


def get_nodes(ctx):
    nodes = k8s.CoreV1Api(ctx.api).list_node().items
    rows = []
    for n in nodes:
        labels = n.metadata.labels or {}
        ready = next((c.status for c in n.status.conditions or [] if c.type == 'Ready'), 'Unknown')
        rows.append({
            'name': n.metadata.name,
            'ready': ready,
            'instance_type': labels.get('node.kubernetes.io/instance-type'),
            'zone': labels.get('topology.kubernetes.io/zone'),
            'nodegroup': labels.get('eks.amazonaws.com/nodegroup'),
            'capacity_type': labels.get('eks.amazonaws.com/capacityType'),
            'internal_ip': next((a.address for a in n.status.addresses if a.type == 'InternalIP'), None),
            'allocatable': {'cpu': n.status.allocatable.get('cpu'), 'memory': n.status.allocatable.get('memory'),
                            'pods': n.status.allocatable.get('pods')},
            'kubelet_version': n.status.node_info.kubelet_version,
            'age': _age(n.metadata.creation_timestamp),
        })
    return {'nodes': rows}


def _cpu_m(q):
    return round(float(parse_quantity(q)) * 1000)


def _mem_mi(q):
    return round(float(parse_quantity(q)) / (1024 ** 2))


def get_resource_usage(ctx, namespace=None):
    custom = k8s.CustomObjectsApi(ctx.api)
    try:
        node_metrics = custom.list_cluster_custom_object('metrics.k8s.io', 'v1beta1', 'nodes')['items']
        if namespace:
            pod_metrics = custom.list_namespaced_custom_object('metrics.k8s.io', 'v1beta1', namespace, 'pods')['items']
        else:
            pod_metrics = custom.list_cluster_custom_object('metrics.k8s.io', 'v1beta1', 'pods')['items']
    except ApiException as e:
        if e.status in (404, 503):
            return {'error': 'Metrics API (metrics.k8s.io) is not available: metrics-server is not installed '
                             'or not ready. Install it, e.g. as the EKS add-on "metrics-server".'}
        raise
    allocatable = {n.metadata.name: n.status.allocatable for n in k8s.CoreV1Api(ctx.api).list_node().items}
    nodes = []
    for m in node_metrics:
        name, alloc = m['metadata']['name'], allocatable.get(m['metadata']['name'], {})
        cpu, mem = _cpu_m(m['usage']['cpu']), _mem_mi(m['usage']['memory'])
        nodes.append({
            'node': name, 'cpu_millicores': cpu, 'memory_mib': mem,
            'cpu_percent_of_allocatable': round(100 * cpu / _cpu_m(alloc['cpu']), 1) if alloc else None,
            'memory_percent_of_allocatable': round(100 * mem / _mem_mi(alloc['memory']), 1) if alloc else None,
        })
    pods = [{
        'namespace': m['metadata']['namespace'], 'pod': m['metadata']['name'],
        'cpu_millicores': sum(_cpu_m(c['usage']['cpu']) for c in m['containers']),
        'memory_mib': sum(_mem_mi(c['usage']['memory']) for c in m['containers']),
    } for m in pod_metrics]
    return {'source': 'metrics-server (live, ~15s window)', 'nodes': nodes, 'pods': pods}


def get_events(ctx, namespace=None, warnings_only=False):
    core = k8s.CoreV1Api(ctx.api)
    events = core.list_namespaced_event(namespace).items if namespace else core.list_event_for_all_namespaces().items
    if warnings_only:
        events = [e for e in events if e.type == 'Warning']
    events.sort(key=lambda e: e.last_timestamp or e.event_time or e.metadata.creation_timestamp, reverse=True)
    return {'events': [{
        'namespace': e.metadata.namespace, 'type': e.type, 'reason': e.reason,
        'object': f'{e.involved_object.kind}/{e.involved_object.name}', 'message': e.message,
        'count': e.count, 'last_seen': _age(e.last_timestamp or e.event_time or e.metadata.creation_timestamp),
    } for e in events[:40]]}


def get_pod_logs(ctx, pod, namespace='default', tail_lines=50):
    logs = k8s.CoreV1Api(ctx.api).read_namespaced_pod_log(pod, namespace, tail_lines=min(int(tail_lines), 200))
    return {'pod': pod, 'namespace': namespace, 'logs': logs}


# --- AWS -------------------------------------------------------------------

def describe_cluster(ctx):
    info = aws.cluster_info(ctx.session)
    if info is None:
        return {'error': 'eks:DescribeCluster is not permitted for the signed-in identity.'}
    eks = ctx.session.client('eks')
    nodegroups = []
    for ng in eks.list_nodegroups(clusterName=aws.CLUSTER)['nodegroups']:
        d = eks.describe_nodegroup(clusterName=aws.CLUSTER, nodegroupName=ng)['nodegroup']
        nodegroups.append({'name': ng, 'status': d['status'], 'instance_types': d.get('instanceTypes'),
                           'capacity_type': d.get('capacityType'), 'scaling': d.get('scalingConfig'),
                           'ami_type': d.get('amiType'), 'subnets': d.get('subnets')})
    vpc = info['resourcesVpcConfig']
    return {
        'name': info['name'], 'status': info['status'], 'kubernetes_version': info['version'],
        'region': aws.REGION, 'endpoint': info['endpoint'],
        'endpoint_public_access': vpc.get('endpointPublicAccess'),
        'endpoint_private_access': vpc.get('endpointPrivateAccess'),
        'public_access_cidrs': vpc.get('publicAccessCidrs'),
        'vpc_id': vpc.get('vpcId'), 'subnet_ids': vpc.get('subnetIds'),
        'cluster_security_group': vpc.get('clusterSecurityGroupId'),
        'service_ipv4_cidr': info.get('kubernetesNetworkConfig', {}).get('serviceIpv4Cidr'),
        'authentication_mode': info.get('accessConfig', {}).get('authenticationMode'),
        'nodegroups': nodegroups,
    }


def _name_tag(resource):
    return next((t['Value'] for t in resource.get('Tags', []) if t['Key'] == 'Name'), None)


def describe_network(ctx):
    info = aws.cluster_info(ctx.session)
    vpc_id = info['resourcesVpcConfig']['vpcId'] if info else os.getenv('VPC_ID')
    if not vpc_id:
        return {'error': 'Cannot determine the VPC: DescribeCluster denied and VPC_ID not set.'}
    ec2 = ctx.session.client('ec2')
    flt = [{'Name': 'vpc-id', 'Values': [vpc_id]}]
    vpc = ec2.describe_vpcs(VpcIds=[vpc_id])['Vpcs'][0]
    subnets = ec2.describe_subnets(Filters=flt)['Subnets']
    route_tables = ec2.describe_route_tables(Filters=flt)['RouteTables']
    igws = ec2.describe_internet_gateways(Filters=[{'Name': 'attachment.vpc-id', 'Values': [vpc_id]}])['InternetGateways']
    nats = ec2.describe_nat_gateways(Filter=flt)['NatGateways']

    subnet_routes = {}
    main_routes = None
    for rt in route_tables:
        routes = [{'destination': r.get('DestinationCidrBlock') or r.get('DestinationIpv6CidrBlock'),
                   'target': r.get('GatewayId') or r.get('NatGatewayId') or r.get('TransitGatewayId') or 'other'}
                  for r in rt['Routes']]
        for assoc in rt.get('Associations', []):
            if assoc.get('Main'):
                main_routes = routes
            if assoc.get('SubnetId'):
                subnet_routes[assoc['SubnetId']] = {'route_table': rt['RouteTableId'], 'routes': routes}
    return {
        'vpc': {'id': vpc_id, 'cidr': vpc['CidrBlock'], 'name': _name_tag(vpc)},
        'subnets': [{
            'id': s['SubnetId'], 'name': _name_tag(s), 'az': s['AvailabilityZone'], 'cidr': s['CidrBlock'],
            'auto_assign_public_ip': s['MapPublicIpOnLaunch'],
            'elb_role_tags': [t['Key'] for t in s.get('Tags', []) if 'kubernetes.io/role' in t['Key']] or None,
            'route_table': subnet_routes.get(s['SubnetId'], {'route_table': 'main', 'routes': main_routes}),
        } for s in subnets],
        'internet_gateways': [g['InternetGatewayId'] for g in igws],
        'nat_gateways': [{'id': n['NatGatewayId'], 'subnet': n['SubnetId'], 'state': n['State'],
                          'public_ip': next((a.get('PublicIp') for a in n['NatGatewayAddresses']), None)}
                         for n in nats],
    }


def describe_load_balancers(ctx):
    hostnames = {h: f"{s['namespace']}/{s['name']}" for s in get_services(ctx)['services']
                 for h in s['load_balancer_hostnames'] or []}
    if not hostnames:
        return {'load_balancers': [], 'note': 'No Kubernetes Service of type LoadBalancer has an address.'}
    results = []
    elbv2 = ctx.session.client('elbv2')
    for lb in elbv2.describe_load_balancers()['LoadBalancers']:
        if lb['DNSName'] not in hostnames:
            continue
        listeners = elbv2.describe_listeners(LoadBalancerArn=lb['LoadBalancerArn'])['Listeners']
        target_groups = []
        for tg in elbv2.describe_target_groups(LoadBalancerArn=lb['LoadBalancerArn'])['TargetGroups']:
            health = elbv2.describe_target_health(TargetGroupArn=tg['TargetGroupArn'])['TargetHealthDescriptions']
            target_groups.append({
                'name': tg['TargetGroupName'], 'protocol': tg['Protocol'], 'port': tg['Port'],
                'target_type': tg['TargetType'],
                'targets': [{'id': h['Target']['Id'], 'port': h['Target'].get('Port'),
                             'health': h['TargetHealth']['State']} for h in health],
            })
        results.append({
            'k8s_service': hostnames.pop(lb['DNSName']), 'dns_name': lb['DNSName'], 'type': lb['Type'],
            'scheme': lb['Scheme'], 'state': lb['State']['Code'], 'subnets_azs':
                [{'az': a['ZoneName'], 'subnet': a['SubnetId']} for a in lb['AvailabilityZones']],
            'listeners': [{'protocol': l['Protocol'], 'port': l['Port']} for l in listeners],
            'target_groups': target_groups,
        })
    if hostnames:  # remaining ones are Classic Load Balancers
        elb = ctx.session.client('elb')
        for lb in elb.describe_load_balancers()['LoadBalancerDescriptions']:
            if lb['DNSName'] not in hostnames:
                continue
            health = elb.describe_instance_health(LoadBalancerName=lb['LoadBalancerName'])['InstanceStates']
            results.append({
                'k8s_service': hostnames.pop(lb['DNSName']), 'dns_name': lb['DNSName'], 'type': 'classic',
                'scheme': lb['Scheme'], 'subnets': lb['Subnets'],
                'listeners': [{'lb_port': l['Listener']['LoadBalancerPort'], 'protocol': l['Listener']['Protocol'],
                               'instance_port': l['Listener']['InstancePort']} for l in lb['ListenerDescriptions']],
                'instances': [{'id': i['InstanceId'], 'state': i['State']} for i in health],
            })
    return {'load_balancers': results, 'unmatched_hostnames': list(hostnames) or None}


# --- Configuration files (Terraform / manifests) ----------------------------

CONFIG_EXTENSIONS = {'.tf', '.tfvars', '.yaml', '.yml', '.json', '.hcl', '.md'}
SKIP_PARTS = {'.terraform', 'node_modules', '.git'}


def _config_roots():
    roots = []
    for d in os.getenv('CONFIG_DIRS', '../infra,../k8s').split(','):
        p = (settings.BASE_DIR / d.strip()).resolve()
        if p.is_dir():
            roots.append(p)
    return roots


def _config_files():
    for root in _config_roots():
        for f in sorted(root.rglob('*')):
            if (f.is_file() and f.suffix in CONFIG_EXTENSIONS and not SKIP_PARTS & set(f.parts)
                    and 'tfstate' not in f.name):  # state files can hold secrets
                yield root, f


def _display(root, f):
    return str(Path(root.name) / f.relative_to(root))


def list_config_files(ctx):
    files = [_display(r, f) for r, f in _config_files()]
    if not files:
        return {'files': [], 'note': 'No config directories found. Set CONFIG_DIRS in backend/.env.'}
    return {'files': files}


def read_config_file(ctx, path):
    for root, f in _config_files():
        if _display(root, f) == path:
            lines = f.read_text(errors='replace').splitlines()
            return {'path': path, 'content': '\n'.join(f'{i:4} | {l}' for i, l in enumerate(lines, 1))}
    return {'error': f'{path} is not a known config file. Use list_config_files.'}


def search_config(ctx, query):
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    matches = []
    for root, f in _config_files():
        for i, line in enumerate(f.read_text(errors='replace').splitlines(), 1):
            if pattern.search(line):
                matches.append({'file': _display(root, f), 'line': i, 'text': line.strip()})
    return {'query': query, 'matches': matches[:50]}


# --- Registry --------------------------------------------------------------

def _fn(name, description, properties=None, required=None):
    return {'type': 'function', 'function': {
        'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties or {}, 'required': required or []},
    }}


_NS = {'namespace': {'type': 'string', 'description': 'Namespace; omit for all namespaces.'}}

TOOLS = {
    'list_pods': (list_pods, _fn('list_pods', 'Live pods with phase, readiness, restarts, node and IP.', _NS)),
    'get_deployments': (get_deployments, _fn(
        'get_deployments', 'Live deployments: desired/ready replicas, images, resources, HPA, how they are managed.', _NS)),
    'get_services': (get_services, _fn(
        'get_services', 'Live Kubernetes Services: type, ports, nodePorts, selectors, load balancer hostnames, ready endpoints.', _NS)),
    'get_nodes': (get_nodes, _fn('get_nodes', 'Worker nodes: readiness, instance type, AZ, nodegroup, allocatable CPU/memory.')),
    'get_resource_usage': (get_resource_usage, _fn(
        'get_resource_usage', 'Current CPU and memory usage of nodes and pods from metrics-server.', _NS)),
    'get_events': (get_events, _fn('get_events', 'Recent Kubernetes events, useful for troubleshooting.', {
        **_NS, 'warnings_only': {'type': 'boolean'}})),
    'get_pod_logs': (get_pod_logs, _fn('get_pod_logs', 'Last lines of a pod\'s logs.', {
        'pod': {'type': 'string'}, 'namespace': {'type': 'string'}, 'tail_lines': {'type': 'integer'}}, ['pod'])),
    'describe_cluster': (describe_cluster, _fn(
        'describe_cluster', 'EKS cluster details from AWS: version, endpoint access, VPC, subnets, node groups and scaling config.')),
    'describe_network': (describe_network, _fn(
        'describe_network', 'AWS VPC networking for the cluster: VPC, subnets, route tables, internet and NAT gateways.')),
    'describe_load_balancers': (describe_load_balancers, _fn(
        'describe_load_balancers', 'AWS load balancers backing Kubernetes LoadBalancer Services: listeners, targets, health.')),
    'list_config_files': (list_config_files, _fn(
        'list_config_files', 'List the Terraform and Kubernetes manifest files of this project.')),
    'read_config_file': (read_config_file, _fn('read_config_file', 'Read a config file with line numbers.', {
        'path': {'type': 'string', 'description': 'Path exactly as returned by list_config_files.'}}, ['path'])),
    'search_config': (search_config, _fn('search_config', 'Case-insensitive text search across config files.', {
        'query': {'type': 'string'}}, ['query'])),
}

TOOL_SCHEMAS = [schema for _, schema in TOOLS.values()]

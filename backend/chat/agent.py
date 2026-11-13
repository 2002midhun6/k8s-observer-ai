"""
The chat agent: OpenAI tool-calling loop over the read-only tools.
"""
import json
import os

from kubernetes.client.exceptions import ApiException
from openai import OpenAI

from . import aws
from .tools import TOOL_SCHEMAS, TOOLS, Context

MODEL = os.getenv('OPENAI_MODEL', 'gpt-5.4-mini')
MAX_STEPS = 8
MAX_TOOL_OUTPUT = 15000

SYSTEM_PROMPT = f"""You are an assistant for one AWS EKS setup. You answer questions about its
architecture, configuration and live status.

Setup: EKS cluster "{aws.CLUSTER}" in region {aws.REGION}, created with Terraform.
You have read-only tools for live Kubernetes data, AWS resources and the project's
config files (Terraform and Kubernetes manifests). You cannot change anything.

Rules:
- Base every factual statement on tool results or config files. Call tools instead of guessing.
- For "how does traffic reach..." questions, trace the full path with real names/ports:
  internet -> load balancer (listeners) -> nodes (nodePort) / targets -> Service -> pods (targetPort).
- For "where can I change ..." questions, search the config files and cite file:line. Also mention
  the live value and how to apply a change (e.g. edit the file, then kubectl apply / terraform apply).
  If no config file defines it, say so and explain where the live value comes from.
- Give numbers with units. Distinguish application pods from kube-system pods.
- If a tool returns an error (e.g. missing permission, metrics-server not installed), say exactly
  what is missing and how to fix it.
- Be concise. Use short lists or a small table when it helps."""


def _run_tool(ctx, name, arguments):
    if name not in TOOLS:
        return {'error': f'Unknown tool {name}'}
    fn, _ = TOOLS[name]
    try:
        return fn(ctx, **arguments)
    except aws.ReadOnlyViolation as e:
        return {'error': str(e)}
    except ApiException as e:
        hint = ''
        if e.status == 401:
            hint = ' The signed-in IAM identity is not mapped into the cluster (add an EKS access entry).'
        elif e.status == 403:
            hint = ' The signed-in identity lacks Kubernetes RBAC permission for this.'
        return {'error': f'Kubernetes API {e.status} {e.reason}.{hint}'}
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}'}


def chat(messages):
    """messages: [{'role': 'user'|'assistant', 'content': str}]. Returns (reply, tool_calls)."""
    client = OpenAI()
    ctx = Context()
    convo = [{'role': 'system', 'content': SYSTEM_PROMPT}] + [
        {'role': m['role'], 'content': m['content']} for m in messages if m.get('role') in ('user', 'assistant')
    ]
    used = []
    for _ in range(MAX_STEPS):
        response = client.chat.completions.create(model=MODEL, messages=convo, tools=TOOL_SCHEMAS)
        msg = response.choices[0].message
        print(msg)
        if not msg.tool_calls:
            return msg.content, used
        convo.append(msg.model_dump(exclude_none=True))
        for call in msg.tool_calls:
            args = json.loads(call.function.arguments or '{}')
            result = _run_tool(ctx, call.function.name, args)
            used.append({'tool': call.function.name, 'args': args, 'error': result.get('error')})
            content = json.dumps(result, default=str)
            if len(content) > MAX_TOOL_OUTPUT:
                content = content[:MAX_TOOL_OUTPUT] + '... [truncated]'
            convo.append({'role': 'tool', 'tool_call_id': call.id, 'content': content})
    return 'I could not finish answering within the tool-call limit. Try a more specific question.', used

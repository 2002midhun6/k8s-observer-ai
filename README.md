# K8s Observer AI

A read-only AI chat assistant for an **AWS EKS** setup. Ask questions in plain English about the cluster's architecture, configuration, and live status, and the assistant answers using real data from Kubernetes, AWS, and the project's Terraform and Kubernetes config files.

It **cannot change anything**. Every tool is read-only, and a guard blocks any write operation.

## What It Does

- Answers questions about a running EKS cluster using live data, not guesses
- Traces how traffic reaches a service, with real names and ports:
  `internet -> load balancer (listeners) -> nodes (nodePort) / targets -> Service -> pods (targetPort)`
- Answers "where can I change X?" by searching Terraform and Kubernetes files and citing `file:line`, then showing the live value and how to apply a change (`kubectl apply` / `terraform apply`)
- Separates application pods from `kube-system` pods and reports numbers with units
- Explains tool errors clearly and says how to fix them (missing permission, metrics-server not installed, and so on)

## Example Questions

- "How does traffic reach my app from the internet?"
- "Which pods are using the most memory?"
- "Where can I change the number of replicas?"
- "What nodes are in the cluster and what instance types are they?"
- "Why can't I see any pods?"

## How It Works

The assistant runs an **OpenAI tool-calling loop** in `agent.py`:

1. The user's chat messages are sent to the model together with a system prompt and a list of read-only tool schemas.
2. If the model requests tools, the agent runs them and sends the results back to the model.
3. This repeats until the model gives a final answer, up to a maximum of 8 steps.
4. The reply is returned along with the list of tools that were used.

```
User question
     |
     v
 chat() in agent.py  <------------------+
     |                                  |
     v                                  |
 OpenAI model --(tool call)--> _run_tool() --> Kubernetes API / AWS / config files
     |                                  |
     +--(final answer)--> reply         +-- tool result (truncated if too long)
```

## Safety and Error Handling

- **Read-only by design.** Tools only read data. A `ReadOnlyViolation` error is raised if anything tries to change AWS resources.
- **Step limit.** The loop stops after 8 tool-call rounds and asks for a more specific question.
- **Output limit.** Tool output is truncated at 15,000 characters so one large result cannot flood the model.
- **Helpful Kubernetes errors.**
  - `401` means the signed-in IAM identity is not mapped into the cluster. Add an EKS access entry.
  - `403` means the identity lacks Kubernetes RBAC permission for the request.

## Tech Stack

- Python
- OpenAI API (tool calling)
- Kubernetes Python client
- AWS SDK (boto3)
- Terraform and Kubernetes manifests as the config sources

## Project Structure

```
k8s-observer-ai/
  agent.py     # chat loop: OpenAI tool calling over the read-only tools
  tools.py     # tool functions and their schemas (TOOLS, TOOL_SCHEMAS, Context)
  aws.py       # AWS helpers, cluster name and region, read-only guard
```

> Update this tree to match the actual repository layout.

## Prerequisites

- Python 3.10+
- An AWS EKS cluster, plus AWS credentials that can read it
- An OpenAI API key
- `kubectl` access to the cluster (the signed-in IAM identity needs an EKS access entry and read permissions)

## Setup

1. Clone the repository:

   ```bash
   git clone https://github.com/2002midhun6/k8s-observer-ai.git
   cd k8s-observer-ai
   ```

2. Create a virtual environment and install dependencies:

   ```bash
   python -m venv venv
   source venv/bin/activate        # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Set environment variables:

   ```bash
   export OPENAI_API_KEY=your_openai_api_key
   export OPENAI_MODEL=gpt-5.4-mini      # optional, this is the default
   export AWS_REGION=your_region
   export AWS_PROFILE=your_profile       # or use other AWS credentials
   ```

4. Make sure your AWS identity can reach the cluster:

   ```bash
   aws eks update-kubeconfig --name your-cluster-name --region your-region
   kubectl get pods -A
   ```

5. Start the app:

   ```bash
   # replace with the real start command for this project
   python manage.py runserver
   ```

> Replace the start command and variable names above with the ones this project really uses.

## Required Permissions

The assistant only needs read access.

- **AWS (IAM):** describe permissions for EKS, EC2, and load balancers
- **Kubernetes (RBAC):** `get` / `list` on pods, services, nodes, deployments, and similar resources
- **Metrics (optional):** `metrics-server` installed in the cluster for CPU and memory usage

## Future Improvements

- Streaming responses to the UI
- Saving conversation history
- More tools, such as logs, events, and ingress details
- Unit tests for the tool functions and the agent loop

## License

Add a license here (for example, MIT).

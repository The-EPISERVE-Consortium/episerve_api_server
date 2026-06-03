#!/bin/bash
set -e

cd "$(dirname "$0")"

# Create venv if missing
if [ ! -d .venv ]; then
    echo "Creating virtual environment..."
    python -m venv .venv
fi

source .venv/bin/activate

# Install / sync dependencies
pip install -r requirements.txt -q

# Load .env if present (non-secret config)
if [ -f .env ]; then
    echo "Loading .env..."
    set -a
    source .env
    set +a
fi

# Pull secrets from Kubernetes if kubectl is available
if command -v kubectl &> /dev/null; then
    echo "Pulling secrets from Kubernetes..."
    export LAKEFS_ACCESS_KEY=$(kubectl get secret lakefs-credentials -o jsonpath='{.data.lakefs-access-key}' | base64 -d)
    export LAKEFS_SECRET_KEY=$(kubectl get secret lakefs-credentials -o jsonpath='{.data.lakefs-secret-key}' | base64 -d)
    export CKAN_API_TOKEN=$(kubectl get secret ckan-credentials -o jsonpath='{.data.ckan-api-token}' | base64 -d)
    echo "Secrets loaded."
else
    echo "kubectl not found — skipping K8s secrets. Set LAKEFS_ACCESS_KEY, LAKEFS_SECRET_KEY, CKAN_API_TOKEN manually."
fi

echo ""
echo "Starting EPISERVE API server at http://localhost:8000"
echo "Docs at http://localhost:8000/docs"
echo ""

PYTHONPATH=. uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

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

# Load .env if present
if [ -f .env ]; then
    echo "Loading .env..."
    set -a
    source .env
    set +a
fi

echo "Starting EPISERVE API server at http://localhost:8000"
echo "Docs at http://localhost:8000/docs"
echo ""

PYTHONPATH=. uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

#!/bin/sh
# Script for Linux/MacOS (bash/zsh)

# Poetry is the only supported install path, so fail early with a useful hint.
if ! command -v poetry >/dev/null 2>&1; then
    echo "Poetry is required. Install it from https://python-poetry.org/docs/#installation" >&2
    exit 1
fi

# Create virtual environment if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "Setting up virtual environment..."
    python3 -m venv .venv
fi

# Activate virtual environment
. .venv/bin/activate

echo "Installing with Poetry..."
poetry install

# Run discovery
python -m evse_discovery

# Deactivate virtual environment
deactivate

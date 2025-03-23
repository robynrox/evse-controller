#!/bin/sh
# Script for Linux/MacOS (bash/zsh)

# Create virtual environment if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "Setting up virtual environment..."
    python3 -m venv .venv
fi

# Activate virtual environment
source .venv/bin/activate

# Check if Poetry is available
if command -v poetry >/dev/null 2>&1; then
    echo "Installing with Poetry..."
    poetry install
else
    echo "Installing with pip..."
    if [ ! -d ".venv/lib/python*/site-packages/pymodbus" ]; then
        pip install -r requirements.txt
    fi
fi

# Run discovery
python -m evse_discovery

# Deactivate virtual environment
deactivate
#!/bin/sh
# Script for Linux/MacOS (bash/zsh)

# Create virtual environment if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "Setting up virtual environment..."
    python3 -m venv .venv
fi

# Activate virtual environment
source .venv/bin/activate

# Install dependencies if needed
if [ ! -f ".venv/lib/python*/site-packages/pymodbus" ]; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
fi

# Run discovery
python -m evse_discovery

# Deactivate virtual environment
deactivate
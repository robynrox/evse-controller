# Script for Windows (PowerShell)

# Poetry is the only supported install path, so fail early with a useful hint.
if (-not (Get-Command poetry -ErrorAction SilentlyContinue)) {
    Write-Error "Poetry is required. Install it from https://python-poetry.org/docs/#installation"
    exit 1
}

# Create virtual environment if it doesn't exist
if (-not (Test-Path ".venv")) {
    Write-Host "Setting up virtual environment..."
    python -m venv .venv
}

# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Install dependencies
Write-Host "Installing with Poetry..."
poetry install

# Run discovery
python -m evse_discovery

# Deactivate virtual environment
deactivate

# Script for Windows (PowerShell)

# Create virtual environment if it doesn't exist
if (-not (Test-Path ".venv")) {
    Write-Host "Setting up virtual environment..."
    python -m venv .venv
}

# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Install dependencies if needed
if (-not (Test-Path ".venv\Lib\site-packages\pymodbus")) {
    Write-Host "Installing dependencies..."
    pip install -r requirements.txt
}

# Run discovery
python -m evse_discovery

# Deactivate virtual environment
deactivate
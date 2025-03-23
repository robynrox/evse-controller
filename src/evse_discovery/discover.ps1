# Script for Windows (PowerShell)

# Create virtual environment if it doesn't exist
if (-not (Test-Path ".venv")) {
    Write-Host "Setting up virtual environment..."
    python -m venv .venv
}

# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Check if Poetry is available
$poetryExists = Get-Command poetry -ErrorAction SilentlyContinue

# Install dependencies
if ($poetryExists) {
    Write-Host "Installing with Poetry..."
    poetry install
} else {
    Write-Host "Installing with pip..."
    if (-not (Test-Path ".venv\Lib\site-packages\pymodbus")) {
        pip install -r requirements.txt
    }
}

# Run discovery
python -m evse_discovery

# Deactivate virtual environment
deactivate
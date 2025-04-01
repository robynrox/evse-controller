## Development Setup

### Required Poetry Plugins

Before starting development, install the following Poetry plugins:

```bash
poetry self add poetry-plugin-export
```

This plugin is needed to generate `requirements.txt` files for pip users.

### Updating Requirements

After making changes to `pyproject.toml`, update the requirements files:

```bash
# Generate main requirements.txt
poetry export -f requirements.txt --without-hashes > requirements.txt

# Generate discovery requirements (if changed)
cd src/evse_discovery && poetry export -f requirements.txt --without-hashes > requirements.txt
```
## Development Setup

### Using a dev container

The dev container definition installs and runs Poetry to download and install
the dependencies needed. If you additionally want to use Qwen Code, there is a
script that installs Qwen Code for you in the dev container here:

`tools/install_qwen_in_dev_container.sh`

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

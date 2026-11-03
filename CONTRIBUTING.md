## Development Setup

### Using a dev container

The dev container definition installs and runs Poetry to download and install
the dependencies needed. If you additionally want to use Qwen Code, there is a
script that installs Qwen Code for you in the dev container here:

`tools/install_qwen_in_dev_container.sh`

### Dependencies

The controller uses Poetry as its only install path; there is no checked-in
`requirements.txt`. Run `poetry install` from the repository root, and change
dependencies in `pyproject.toml`; Poetry updates `poetry.lock` for you.

The standalone discovery tool in `src/evse_discovery` is its own Poetry project
with its own `pyproject.toml` and `poetry.lock`. Its `discover.sh` /
`discover.ps1` scripts require Poetry and install from that lock.

If you need a pip-style list for an environment that cannot use Poetry, install
`poetry-plugin-export` (`poetry self add poetry-plugin-export`) and generate one
locally. Do not commit it; the checked-in lock file is the source of truth.

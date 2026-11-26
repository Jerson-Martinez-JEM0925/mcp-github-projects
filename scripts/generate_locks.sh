#!/usr/bin/env bash
# Regenerate the hash-pinned runtime and development requirement locks.
# Intended to run inside the pinned Python Docker image through `make lock`.
set -euo pipefail

printf '%s\n' 'pip==26.3 --hash=sha256:f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13 --hash=sha256:6c36ae7da8695be67e3cd9f7d87919667ced53f57ce2ede75d89b5912ed2bc4c' > /tmp/mcp-pip-bootstrap.lock

python3 -m venv /tmp/mcp-lock-runtime-venv
/tmp/mcp-lock-runtime-venv/bin/pip install --quiet --require-hashes -r /tmp/mcp-pip-bootstrap.lock
/tmp/mcp-lock-runtime-venv/bin/pip install --quiet -r requirements.txt
/tmp/mcp-lock-runtime-venv/bin/pip freeze > /tmp/mcp-runtime-pins.txt

python3 -m venv /tmp/mcp-lock-dev-venv
/tmp/mcp-lock-dev-venv/bin/pip install --quiet --require-hashes -r /tmp/mcp-pip-bootstrap.lock
/tmp/mcp-lock-dev-venv/bin/pip install --quiet -r requirements-dev.txt
/tmp/mcp-lock-dev-venv/bin/pip freeze > /tmp/mcp-dev-pins.txt

python3 scripts/generate_hash_locks.py /tmp/mcp-runtime-pins.txt requirements-runtime.lock
python3 scripts/generate_hash_locks.py /tmp/mcp-dev-pins.txt requirements-dev.lock

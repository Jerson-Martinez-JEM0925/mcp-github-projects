#!/usr/bin/env bash
# Regenerate the hash-pinned runtime and development requirement locks.
# Intended to run inside the pinned Python Docker image through `make lock`.
set -euo pipefail

printf '%s\n' 'pip==26.2.2 --hash=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70 --hash=sha256:3a8e5c800492787f33a4114ef54444546c340df428974ebba17e0fd47e11af2f' > /tmp/mcp-pip-bootstrap.lock

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

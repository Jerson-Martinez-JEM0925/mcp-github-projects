# Stability and compatibility policy

The `1.x` release line is stable. This contract covers registered tool names,
input and output schemas, documented environment variables, access policy,
and structured error categories.

Minor releases may add tools, optional fields, capabilities, documentation, and
backward-compatible validation improvements. Patch releases contain fixes and
security updates only. Removing or renaming a tool, changing a required
parameter, changing the meaning of an access or scope setting, or removing an
error field requires a major release. Deprecated behavior is documented for at
least one minor release before removal.

`tests/test_contracts.py` freezes the schema digest for the stable catalog and
fails if a tool name, description, input schema, or output schema changes
without an intentional digest update. `make validate` is the local release
gate and GitHub Actions must be green before a release tag is pushed.

The live smoke suite uses a configured GitHub token and target Project. It does
not claim that every token permission combination has been tested; the token's
actual permissions remain the operator's responsibility.

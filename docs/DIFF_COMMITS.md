# Diff-based repository commits

`commit_files` accepts either full file contents (`files` / `deletions`) or a
text unified diff (`patch`). Patch mode avoids sending unchanged file contents
through the MCP client.

```json
{
  "branch": "chatbot/my-change",
  "message": "feat(tool): update repository files",
  "patch": "diff --git a/docs/example.md b/docs/example.md\n...",
  "expected_head_sha": "40-character-branch-head-sha"
}
```

Patch mode requires the exact current head SHA. The server rejects stale heads,
unsupported hunk context, malformed or duplicate paths, binary patches,
renames, quoted paths with spaces, and changes outside the configured write
path allowlist. Existing branch-prefix and default-branch protections remain
in force. The commit is still created through GitHub's `createCommitOnBranch`
mutation and must be followed by a reviewed pull request.

The patch and resulting file contents are each limited to 1 MB; at most 50
files may change. Patches may add, update, and delete text files. They cannot
be combined with `files` or `deletions`.

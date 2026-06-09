# Databricks Skills Source

The `databricks-*` skills and `spark-python-data-source` in this directory are
vendored from the upstream [ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit)
`databricks-skills/` directory. Do not hand-edit them — refresh via the
`refresh-databricks-skills` skill instead.

| Field | Value |
|-------|-------|
| Upstream repo | https://github.com/databricks-solutions/ai-dev-kit |
| Source path | `databricks-skills/` |
| Pinned commit | `76c774faec57fbb1b78fc8482ba64bf762bb42b1` |
| Upstream VERSION | `0.1.12` |
| Synced on | 2026-06-09 |
| Synced by | refresh-databricks-skills |

## Not vendored from ai-dev-kit (preserved on refresh)

- `databricks-app-apx` — Control Tower / apx-specific, fork-only
- `refresh-databricks-skills` — the refresh skill itself
- superpowers + bdd workflow skills (`brainstorming`, `test-driven-development`, etc.)

## How to check freshness

Compare `Pinned commit` above against upstream `main`:

```bash
gh api repos/databricks-solutions/ai-dev-kit/commits/main --jq .sha
```

If it differs, run the `refresh-databricks-skills` skill and update this file.

"""Export a Databricks Workspace tree (Git Folder contents) to a local directory.

Used by ``coda_interactive`` to materialize a Workspace Git Folder onto the
Coda container's disk before launching an agent in that directory.

Only the working tree is exported — Git Folder server-side metadata (the
``.git/`` directory) is not exposed by the Workspace API.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def export_workspace_tree(client: Any, workspace_path: str, dest_dir: str) -> None:
    """Export the Workspace tree rooted at ``workspace_path`` into ``dest_dir``.

    ``client`` is a ``databricks.sdk.WorkspaceClient`` (or compatible mock).
    Recursively lists entries, calls ``workspace.export()`` per file with
    ``ExportFormat.SOURCE``, decodes the base64 content, and writes to the
    local mirror.

    Per-file export errors (e.g. binaries that fail SOURCE export) are logged
    and skipped — they do not abort the export. The agent in the session may
    not have access to those files; the human can decide whether that matters.
    """
    os.makedirs(dest_dir, exist_ok=True)

    try:
        from databricks.sdk.service.workspace import ExportFormat
        export_format = ExportFormat.SOURCE
    except Exception:
        export_format = None  # mocks won't care

    _export_recursive(client, workspace_path, dest_dir, export_format)


def _export_recursive(client, workspace_path: str, dest_dir: str, export_format) -> None:
    """Walk one level of the workspace and export files / recurse into dirs."""
    try:
        entries = list(client.workspace.list(workspace_path))
    except Exception as e:
        logger.warning("workspace.list(%s) failed: %s", workspace_path, e)
        return

    for entry in entries:
        rel_name = os.path.basename(entry.path)
        local_path = os.path.join(dest_dir, rel_name)
        object_type = str(getattr(entry, "object_type", ""))

        if object_type == "DIRECTORY" or object_type.endswith(".DIRECTORY"):
            os.makedirs(local_path, exist_ok=True)
            _export_recursive(client, entry.path, local_path, export_format)
        elif object_type == "FILE" or object_type.endswith(".FILE") or object_type == "NOTEBOOK" or object_type.endswith(".NOTEBOOK"):
            try:
                if export_format is not None:
                    exported = client.workspace.export(path=entry.path, format=export_format)
                else:
                    exported = client.workspace.export(path=entry.path)
                content_b64 = getattr(exported, "content", "") or ""
                content_bytes = base64.b64decode(content_b64) if content_b64 else b""
                with open(local_path, "wb") as f:
                    f.write(content_bytes)
            except Exception as e:
                logger.warning("workspace.export(%s) failed; skipping: %s", entry.path, e)
                continue
        else:
            # Unknown object type; skip with a log line.
            logger.info("Skipping unknown object_type=%r at %s", object_type, entry.path)

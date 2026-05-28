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

try:
    from databricks.sdk.service.workspace import ExportFormat, ObjectType
    _EXPORT_FORMAT = ExportFormat.SOURCE
except Exception:
    ExportFormat = None
    ObjectType = None
    _EXPORT_FORMAT = None


_NOTEBOOK_LANG_TO_EXT = {
    "PYTHON": ".py",
    "SCALA": ".scala",
    "SQL": ".sql",
    "R": ".r",
}


def _is_directory(entry):
    if ObjectType is not None and getattr(entry, "object_type", None) == ObjectType.DIRECTORY:
        return True
    # Fallback for mocks / string-typed entries
    ot = str(getattr(entry, "object_type", ""))
    return ot == "DIRECTORY" or ot.endswith(".DIRECTORY")


def _is_file_or_notebook(entry):
    if ObjectType is not None:
        if getattr(entry, "object_type", None) in (ObjectType.FILE, ObjectType.NOTEBOOK):
            return True
    ot = str(getattr(entry, "object_type", ""))
    return ot in ("FILE", "NOTEBOOK") or ot.endswith(".FILE") or ot.endswith(".NOTEBOOK")


def _is_notebook(entry):
    if ObjectType is not None and getattr(entry, "object_type", None) == ObjectType.NOTEBOOK:
        return True
    ot = str(getattr(entry, "object_type", ""))
    return ot == "NOTEBOOK" or ot.endswith(".NOTEBOOK")


def _local_path_for(entry, dest_dir):
    """Compute local file path for an entry, appending language-based extension for notebooks."""
    rel_name = os.path.basename(entry.path)
    # Only mutate for notebooks; FILEs already have their extension in the path.
    if _is_notebook(entry):
        language = str(getattr(entry, "language", "")).upper()
        ext = _NOTEBOOK_LANG_TO_EXT.get(language, ".py")  # default .py if language missing
        if not rel_name.endswith(ext):
            rel_name = rel_name + ext
    return os.path.join(dest_dir, rel_name)


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
    _export_recursive(client, workspace_path, dest_dir, _EXPORT_FORMAT)


def _export_recursive(client, workspace_path: str, dest_dir: str, export_format) -> None:
    """Walk one level of the workspace and export files / recurse into dirs."""
    try:
        entries = list(client.workspace.list(workspace_path))
    except Exception as e:
        logger.warning("workspace.list(%s) failed: %s", workspace_path, e)
        return

    for entry in entries:
        if _is_directory(entry):
            sub_local = os.path.join(dest_dir, os.path.basename(entry.path))
            os.makedirs(sub_local, exist_ok=True)
            _export_recursive(client, entry.path, sub_local, export_format)
        elif _is_file_or_notebook(entry):
            local_path = _local_path_for(entry, dest_dir)
            try:
                if export_format is not None:
                    exported = client.workspace.export(path=entry.path, format=export_format)
                else:
                    exported = client.workspace.export(path=entry.path)
                content_b64 = getattr(exported, "content", None) or ""
                content_bytes = base64.b64decode(content_b64) if content_b64 else b""
                with open(local_path, "wb") as f:
                    f.write(content_bytes)
            except Exception as e:
                logger.warning("workspace.export(%s) failed; skipping: %s", entry.path, e)
                continue
        else:
            # Unknown object type; skip with a warning.
            object_type = str(getattr(entry, "object_type", ""))
            logger.warning("Skipping unknown object_type=%r at %s", object_type, entry.path)

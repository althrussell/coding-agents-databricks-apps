"""Tests for coda_mcp.workspace_export.export_workspace_tree."""
import os
from unittest.mock import MagicMock, patch

import pytest

from coda_mcp.workspace_export import export_workspace_tree


def _fake_object(path, object_type):
    """Minimal stand-in for databricks.sdk.service.workspace.ObjectInfo."""
    o = MagicMock()
    o.path = path
    o.object_type = object_type
    return o


def test_export_workspace_tree_creates_dest_dir(tmp_path):
    """Helper creates the destination directory if it doesn't exist."""
    dest = tmp_path / "subdir"
    assert not dest.exists()

    client = MagicMock()
    client.workspace.list.return_value = []
    export_workspace_tree(client, "/Workspace/Users/x/empty", str(dest))

    assert dest.exists() and dest.is_dir()


def test_export_workspace_tree_writes_single_file(tmp_path):
    """A workspace with one file gets that file written to the local dir."""
    client = MagicMock()
    client.workspace.list.return_value = [
        _fake_object("/Workspace/Users/x/proj/main.py", "FILE"),
    ]
    # Export returns an object with .content (base64-encoded bytes)
    import base64
    mock_export = MagicMock()
    mock_export.content = base64.b64encode(b"print('hi')\n").decode("ascii")
    client.workspace.export.return_value = mock_export

    export_workspace_tree(client, "/Workspace/Users/x/proj", str(tmp_path))

    main_py = tmp_path / "main.py"
    assert main_py.exists()
    assert main_py.read_text() == "print('hi')\n"


def test_export_workspace_tree_handles_nested_dirs(tmp_path):
    """Nested directory structure is preserved in the destination."""
    client = MagicMock()
    # First list call returns the top-level entries
    # Subsequent recursive calls return the subdir contents
    def list_side_effect(path, **kwargs):
        if path == "/Workspace/Users/x/proj":
            return [
                _fake_object("/Workspace/Users/x/proj/main.py", "FILE"),
                _fake_object("/Workspace/Users/x/proj/lib", "DIRECTORY"),
            ]
        elif path == "/Workspace/Users/x/proj/lib":
            return [
                _fake_object("/Workspace/Users/x/proj/lib/util.py", "FILE"),
            ]
        return []
    client.workspace.list.side_effect = list_side_effect

    import base64
    def export_side_effect(path, **kwargs):
        mock = MagicMock()
        if path.endswith("main.py"):
            mock.content = base64.b64encode(b"main\n").decode("ascii")
        else:
            mock.content = base64.b64encode(b"util\n").decode("ascii")
        return mock
    client.workspace.export.side_effect = export_side_effect

    export_workspace_tree(client, "/Workspace/Users/x/proj", str(tmp_path))

    assert (tmp_path / "main.py").read_text() == "main\n"
    assert (tmp_path / "lib" / "util.py").read_text() == "util\n"


def test_export_workspace_tree_skips_binary_files_gracefully(tmp_path, caplog):
    """Files that fail to export (e.g. binaries) are skipped and logged, not fatal."""
    client = MagicMock()
    client.workspace.list.return_value = [
        _fake_object("/Workspace/Users/x/proj/text.py", "FILE"),
        _fake_object("/Workspace/Users/x/proj/image.png", "FILE"),
    ]

    import base64
    def export_side_effect(path, **kwargs):
        if path.endswith(".png"):
            raise Exception("400 Bad Request: cannot export binary as SOURCE")
        mock = MagicMock()
        mock.content = base64.b64encode(b"hello\n").decode("ascii")
        return mock
    client.workspace.export.side_effect = export_side_effect

    # Should NOT raise; should skip and log.
    export_workspace_tree(client, "/Workspace/Users/x/proj", str(tmp_path))

    assert (tmp_path / "text.py").exists()
    assert not (tmp_path / "image.png").exists()


def test_export_workspace_tree_empty_workspace(tmp_path):
    """Empty workspace path produces empty destination dir (no error)."""
    client = MagicMock()
    client.workspace.list.return_value = []

    export_workspace_tree(client, "/Workspace/Users/x/empty", str(tmp_path))

    assert tmp_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_export_workspace_tree_skips_unknown_object_type(tmp_path, caplog):
    """Unknown object types (e.g. REPO) are skipped with a warning."""
    client = MagicMock()
    client.workspace.list.return_value = [
        _fake_object("/Workspace/Users/x/proj/something", "REPO"),
    ]
    with caplog.at_level("WARNING"):
        export_workspace_tree(client, "/Workspace/Users/x/proj", str(tmp_path))
    # No file should be written
    assert list(tmp_path.iterdir()) == []
    # And a warning should mention the unknown type
    assert any("REPO" in record.getMessage() for record in caplog.records), \
        f"Expected a log warning mentioning the unknown REPO type. Records: {[r.getMessage() for r in caplog.records]}"


def test_export_workspace_tree_appends_extension_for_notebooks(tmp_path):
    """Notebooks get language-based extension appended to the basename."""
    client = MagicMock()
    notebook_entry = MagicMock()
    notebook_entry.path = "/Workspace/Users/x/proj/MyNotebook"
    notebook_entry.object_type = "NOTEBOOK"
    notebook_entry.language = "PYTHON"
    client.workspace.list.return_value = [notebook_entry]

    import base64
    mock_export = MagicMock()
    mock_export.content = base64.b64encode(b"# Databricks notebook source\nprint('hi')\n").decode("ascii")
    client.workspace.export.return_value = mock_export

    export_workspace_tree(client, "/Workspace/Users/x/proj", str(tmp_path))

    # Should have appended .py extension
    expected_path = tmp_path / "MyNotebook.py"
    assert expected_path.exists(), \
        f"Expected MyNotebook.py for Python notebook; got: {list(tmp_path.iterdir())}"

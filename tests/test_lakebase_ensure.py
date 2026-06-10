"""Unit tests for scripts/lakebase_ensure.py.

Exercise the pure helpers and the idempotent on-demand provisioning logic
against a fake duck-typed WorkspaceClient.database service — no network, no real
SDK calls. The on-demand contract (one instance per lab, reused; non-interactive
binding recorded to ~/.coda/lakebase.json) is asserted end to end.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

# Load scripts/lakebase_ensure.py as a module (scripts/ isn't a package).
_SPEC_PATH = Path(__file__).resolve().parent.parent / "scripts" / "lakebase_ensure.py"
_spec = importlib.util.spec_from_file_location("lakebase_ensure", _SPEC_PATH)
lke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lke)  # type: ignore[union-attr]


# ── pure helpers ─────────────────────────────────────────────────────────


def test_resolve_instance_name_precedence():
    assert lke.resolve_instance_name("foo") == "foo"
    assert lke.resolve_instance_name(None, {"LAKEBASE_INSTANCE_NAME": "bar"}) == "bar"
    assert lke.resolve_instance_name(None, {}) == lke.DEFAULT_INSTANCE_NAME
    # arg beats env
    assert lke.resolve_instance_name("foo", {"LAKEBASE_INSTANCE_NAME": "bar"}) == "foo"


def test_parse_tags_empty():
    assert lke.parse_tags({}) == []
    assert lke.parse_tags({"LAB_RESOURCE_TAGS": "  "}) == []


def test_parse_tags_parses_and_skips_malformed():
    tags = lke.parse_tags({"LAB_RESOURCE_TAGS": "lab=ws1, owner=ct ,bad,=novalue"})
    pairs = [(t.key, t.value) for t in tags]
    assert ("lab", "ws1") in pairs
    assert ("owner", "ct") in pairs
    # "bad" (no =) and "=novalue" (empty key) are skipped, never fatal.
    assert all(k for k, _ in pairs)
    assert len(pairs) == 2


# ── fakes ────────────────────────────────────────────────────────────────


class _State:
    def __init__(self, value):
        self.value = value


class _Instance:
    def __init__(self, name, state="AVAILABLE", dns="host.example:5432"):
        self.name = name
        self.state = _State(state)
        self.read_write_dns = dns
        self.custom_tags = None


class _Database:
    def __init__(self, *, existing=None, get_error=None):
        self._existing = existing
        self._get_error = get_error
        self.create_calls = []
        self.waited = []

    def get_database_instance(self, name):
        if self._get_error is not None:
            raise self._get_error
        if self._existing is None:
            from databricks.sdk.errors import NotFound

            raise NotFound(f"{name} not found")
        return self._existing

    def create_database_instance_and_wait(self, instance, timeout=None):
        self.create_calls.append(instance)
        created = _Instance(instance.name, state="AVAILABLE")
        created.custom_tags = getattr(instance, "custom_tags", None)
        self._existing = created
        return created

    def wait_get_database_instance_database_available(self, name):
        self.waited.append(name)
        inst = _Instance(name, state="AVAILABLE")
        self._existing = inst
        return inst


class _FakeClient:
    def __init__(self, database):
        self.database = database


# ── _get_instance ──────────────────────────────────────────────────────────


def test_get_instance_returns_none_on_not_found():
    client = _FakeClient(_Database(existing=None))
    assert lke._get_instance(client, "x") is None


def test_get_instance_reraises_other_errors():
    client = _FakeClient(_Database(get_error=RuntimeError("permission denied")))
    with pytest.raises(RuntimeError, match="permission denied"):
        lke._get_instance(client, "x")


def test_get_instance_treats_generic_notfound_message_as_absent():
    client = _FakeClient(_Database(get_error=RuntimeError("RESOURCE_DOES_NOT_EXIST")))
    assert lke._get_instance(client, "x") is None


# ── ensure_instance idempotency ──────────────────────────────────────────────


def test_ensure_instance_creates_when_absent_with_tags():
    db = _Database(existing=None)
    client = _FakeClient(db)
    tags = lke.parse_tags({"LAB_RESOURCE_TAGS": "lab=ws1"})
    inst = lke.ensure_instance(client, "coda-lab", capacity="CU_1", tags=tags)
    assert lke._state(inst) == "AVAILABLE"
    assert len(db.create_calls) == 1
    # tags propagated onto the created instance
    created = db.create_calls[0]
    assert [(t.key, t.value) for t in created.custom_tags] == [("lab", "ws1")]
    assert created.capacity == "CU_1"


def test_ensure_instance_reuses_available_without_create():
    db = _Database(existing=_Instance("coda-lab", state="AVAILABLE"))
    client = _FakeClient(db)
    inst = lke.ensure_instance(client, "coda-lab")
    assert lke._state(inst) == "AVAILABLE"
    assert db.create_calls == []  # never created a second instance


def test_ensure_instance_waits_when_starting():
    db = _Database(existing=_Instance("coda-lab", state="STARTING"))
    client = _FakeClient(db)
    inst = lke.ensure_instance(client, "coda-lab")
    assert lke._state(inst) == "AVAILABLE"
    assert db.create_calls == []
    assert db.waited == ["coda-lab"]


# ── binding + write_binding ──────────────────────────────────────────────────


def test_binding_shape():
    inst = _Instance("coda-lab", state="AVAILABLE", dns="rw.example:5432")
    data = lke.binding(inst, "coda-lab", database_name="databricks_postgres")
    assert data == {
        "name": "coda-lab",
        "state": "AVAILABLE",
        "read_write_dns": "rw.example:5432",
        "database_name": "databricks_postgres",
    }


def test_write_binding_roundtrip(tmp_path):
    target = tmp_path / ".coda" / "lakebase.json"
    data = {"name": "coda-lab", "state": "AVAILABLE", "read_write_dns": "x", "database_name": "databricks_postgres"}
    out = lke.write_binding(data, path=target)
    assert out == target
    assert json.loads(target.read_text()) == data


# ── main() end-to-end with a fake client ─────────────────────────────────────


def test_main_provisions_binds_and_records(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LAB_RESOURCE_TAGS", "lab=ws9")
    db = _Database(existing=None)
    monkeypatch.setattr(lke, "_make_client", lambda profile: _FakeClient(db))

    rc = lke.main(["--name", "coda-lab"])
    assert rc == 0
    assert len(db.create_calls) == 1

    recorded = json.loads((tmp_path / ".coda" / "lakebase.json").read_text())
    assert recorded["name"] == "coda-lab"
    assert recorded["state"] == "AVAILABLE"
    assert recorded["database_name"] == lke.DEFAULT_DATABASE_NAME


def test_main_nonzero_on_provision_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    db = _Database(get_error=RuntimeError("permission denied: no database-create entitlement"))
    monkeypatch.setattr(lke, "_make_client", lambda profile: _FakeClient(db))

    rc = lke.main(["--name", "coda-lab"])
    assert rc == 1
    # Nothing recorded on failure.
    assert not (tmp_path / ".coda" / "lakebase.json").exists()

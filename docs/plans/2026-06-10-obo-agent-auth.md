# OBO (On-Behalf-Of-User) Agent Auth for Labs — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let a lab attendee use CoDA with **no manual PAT** by authenticating the coding-agent CLIs with the attendee's **OBO user token** (`x-forwarded-access-token`), so everything the agent builds/deploys is owned by the attendee — with the existing **PAT flow kept as a fallback** for unattended/long runs.

**Architecture:** Add a dedicated **`CODA_OBO_ENABLED` gate, on by default**. It is **hard-gated to lab mode**: it only has effect when `CODA_PROFILE=lab` and is ignored in the full profile (OBO's browser-driven refresh only suits attended sessions). Within lab it defaults on and can be disabled with `CODA_OBO_ENABLED=false`. The rest of the plan keys off a derived `_agent_auth_mode()` that returns `obo` when the gate is active, else `pat`. In `obo` mode the app captures `x-forwarded-access-token` on each authenticated inbound request (HTTP + WebSocket connect), pumps it into every agent CLI config via the **existing** `cli_auth.update_cli_tokens()` + `DATABRICKS_TOKEN` pipeline, and triggers setup on first capture. A lightweight **browser keepalive** re-hits an endpoint every ~20 min so the ~60-min user token never goes stale while the tab is open. If no OBO token is ever seen (feature disabled, header missing), the app falls back to the existing PAT prompt.

**Tech Stack:** Python, Flask, Flask-SocketIO, `requests`, `threading`, databricks-sdk (all present).

---

## Background: why OBO, and how auth flows today (read first)

- CoDA's agents don't just call an LLM — they **act on Databricks** (CLI, create jobs/apps/Lakebase, deploy, workspace sync). The bearer token is therefore the agent's **Databricks identity for everything it builds**. OBO makes that identity the **attendee**, so their artifacts are theirs (git identity, workspace files, app/Lakebase ownership all resolve to the user). This is the whole reason to prefer OBO over the app SP for labs.
- Today the bearer is a **user PAT**, entered at `POST /api/configure-pat` (`app.py:1192`), which calls `_configure_all_cli_auth()` (`app.py:405`) and triggers `run_setup()` (`app.py:1264-1270`). `PATRotator` (`pat_rotator.py`) keeps it fresh via `cli_auth.update_cli_tokens()` (`cli_auth.py`).
- **Reuse, don't reinvent:** the mechanism to push a refreshed token into running agents already exists and works in production (`cli_auth.update_cli_tokens()` + `pat_rotator._write_databrickscfg()` + `os.environ["DATABRICKS_TOKEN"]`). OBO reuses it verbatim; only the *source* of the token differs (forwarded header vs PAT mint).

### Verified facts (don't re-litigate)
- OBO can request `all-apis` scope → full API breadth for the agent. (Databricks docs; `App.user_api_scopes`.)
- `x-forwarded-access-token` is the user's downscoped OAuth token, **~60 min lifetime**, **app cannot self-refresh** — a fresh one arrives only on a new inbound HTTP request. (Confirmed via Databricks docs + community.)
- Headless provisioning is possible (Task 7): SDK `App.user_api_scopes` is settable on `apps.create`, and `WorkspaceSettingsV2API.patch_public_workspace_setting` can set `allowed_apps_user_api_scopes.allowed_scopes=["all-apis"]` to enable OBO per workspace.

### Key runtime assumptions / risks
- **R1 — CLIs re-read the rewritten token mid-session.** The existing PAT rotator rewrites `~/.claude/settings.json` etc. every 10 min and works, so the CLIs tolerate token swaps via config rewrite. OBO relies on the same behavior. (Validate in Task 6 smoke test.)
- **R2 — tab-closed long runs.** If the attendee closes the tab / sleeps the laptop while a long agent task runs server-side, the keepalive stops and the token 401s at ~60 min. Mitigation: the PAT fallback (a pasted PAT takes over via the rotator and is self-refreshing).
- **R3 — single gunicorn worker** (per CLAUDE.md) → one in-memory token holder is sufficient. Lab instances are single-user (own workspace each), so no multi-user token juggling.

### Integration points (exact)
- `authorize_request()` — `@app.before_request` at `app.py:1045`.
- `handle_ws_connect()` — `@socketio.on('connect')` at `app.py:773`.
- `check_authorization()` at `app.py:743`; `get_request_user()` at `app.py:613` (already reads `x-forwarded-*` headers).
- `pat_status()` at `app.py:1167`; `configure_pat()` at `app.py:1192`.
- `initialize_app()` at `app.py:1531`; SP-cred strip at `app.py:1551-1555`.

---

### Task 1: Dedicated `CODA_OBO_ENABLED` gate (on by default, lab-gated) + auth-mode resolver

**Files:**
- Modify: `app.py` (add helpers after `_coda_auth_mode()`, ~`app.py:682`)
- Test: `tests/test_agent_auth_mode.py` (create)

**Design:** OBO has its **own dedicated on/off gate**, `CODA_OBO_ENABLED`, which **defaults to `true`**. It is independent of the agent-auth concept, so OBO can be turned off without touching anything else. The gate is **hard-gated to lab mode**: it only has effect when `CODA_PROFILE=lab` and is ignored entirely in the full profile (OBO's browser-driven refresh only suits attended sessions). The string auth-mode used by the rest of the plan derives from this gate: `_agent_auth_mode()` returns `"obo"` iff `_obo_enabled()` else `"pat"`. This keeps Tasks 2-8 (which check `_agent_auth_mode() == "obo"`) unchanged.

**Step 1: Write the failing test**

```python
# tests/test_agent_auth_mode.py
"""CODA_OBO_ENABLED gate (on by default, lab-only) and the derived auth mode."""
import importlib, os
from unittest import mock


def _reload_app():
    import app
    return importlib.reload(app)


# --- the dedicated gate -------------------------------------------------

def test_obo_gate_on_by_default_in_lab():
    with mock.patch.dict(os.environ, {"CODA_PROFILE": "lab"}):
        os.environ.pop("CODA_OBO_ENABLED", None)
        app = _reload_app()
        assert app._obo_enabled() is True
        assert app._agent_auth_mode() == "obo"


def test_obo_gate_can_be_disabled_in_lab():
    with mock.patch.dict(os.environ, {"CODA_PROFILE": "lab", "CODA_OBO_ENABLED": "false"}):
        app = _reload_app()
        assert app._obo_enabled() is False
        assert app._agent_auth_mode() == "pat"


def test_obo_gate_disabled_values(tmp_path):
    for val in ("false", "0", "no", "off", "FALSE", "Off"):
        with mock.patch.dict(os.environ, {"CODA_PROFILE": "lab", "CODA_OBO_ENABLED": val}):
            assert _reload_app()._obo_enabled() is False


# --- hard lab gate (gate has no effect outside lab) ---------------------

def test_obo_gate_ignored_outside_lab_even_if_on():
    with mock.patch.dict(os.environ, {"CODA_PROFILE": "full", "CODA_OBO_ENABLED": "true"}):
        app = _reload_app()
        assert app._obo_enabled() is False
        assert app._agent_auth_mode() == "pat"


def test_obo_gate_ignored_when_no_profile():
    with mock.patch.dict(os.environ, {"CODA_OBO_ENABLED": "true"}):
        os.environ.pop("CODA_PROFILE", None)
        assert _reload_app()._obo_enabled() is False


def test_default_full_profile_is_pat():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CODA_OBO_ENABLED", None)
        os.environ.pop("CODA_PROFILE", None)
        assert _reload_app()._agent_auth_mode() == "pat"
```

**Step 2: Run — verify fail**

Run: `uv run pytest tests/test_agent_auth_mode.py -v`
Expected: FAIL — `AttributeError: module 'app' has no attribute '_obo_enabled'`

**Step 3: Implement** (add after `_coda_auth_mode()`):

```python
_FALSEY = {"false", "0", "no", "off", ""}


def _obo_enabled(env=None):
    """Dedicated gate for OBO agent auth. **On by default.**

    OBO lets the coding-agent CLIs authenticate as the attendee via the forwarded
    user token (x-forwarded-access-token). Its browser-driven refresh model only
    suits attended lab/workshop sessions, so the gate is **hard-gated to lab mode**:
    it is ignored entirely unless ``CODA_PROFILE=lab``. Within lab it defaults ON and
    can be disabled with ``CODA_OBO_ENABLED=false`` (agents then use the PAT flow).
    """
    env = env if env is not None else os.environ
    if _coda_profile(env) != "lab":
        return False  # OBO never engages outside lab mode, regardless of the gate
    return env.get("CODA_OBO_ENABLED", "true").strip().lower() not in _FALSEY


def _agent_auth_mode(env=None):
    """Derived auth mode for the agent CLIs: ``obo`` when the OBO gate is active,
    else ``pat`` (user pastes a PAT; PATRotator keeps it fresh)."""
    return "obo" if _obo_enabled(env) else "pat"
```

**Step 4: Run — verify pass**

Run: `uv run pytest tests/test_agent_auth_mode.py -v` → all PASS

**Step 5: Commit**

```bash
git add app.py tests/test_agent_auth_mode.py
git commit -m "feat: add dedicated CODA_OBO_ENABLED gate (on by default, lab-only)"
```

---

### Task 2: `OBOTokenManager` — capture, dedupe, pump

**Files:**
- Create: `obo_auth.py`
- Test: `tests/test_obo_auth.py` (create)

**Step 1: Write the failing tests**

```python
# tests/test_obo_auth.py
"""OBO user-token capture + pump into agent configs."""
import os
from unittest import mock


class TestCapture:
    def test_update_from_headers_stores_token(self):
        from obo_auth import OBOTokenManager
        m = OBOTokenManager()
        with mock.patch.object(m, "_pump") as pump:
            changed = m.update_from_headers({"x-forwarded-access-token": "tok-1"})
        assert changed is True
        assert m.token == "tok-1"
        pump.assert_called_once_with("tok-1")

    def test_missing_header_is_noop(self):
        from obo_auth import OBOTokenManager
        m = OBOTokenManager()
        with mock.patch.object(m, "_pump") as pump:
            changed = m.update_from_headers({"x-forwarded-email": "a@x.com"})
        assert changed is False
        assert m.token is None
        pump.assert_not_called()

    def test_same_token_does_not_repump(self):
        from obo_auth import OBOTokenManager
        m = OBOTokenManager()
        with mock.patch.object(m, "_pump") as pump:
            m.update_from_headers({"x-forwarded-access-token": "tok-1"})
            m.update_from_headers({"x-forwarded-access-token": "tok-1"})
        assert pump.call_count == 1  # only re-pump on change

    def test_new_token_repumps(self):
        from obo_auth import OBOTokenManager
        m = OBOTokenManager()
        with mock.patch.object(m, "_pump") as pump:
            m.update_from_headers({"x-forwarded-access-token": "tok-1"})
            m.update_from_headers({"x-forwarded-access-token": "tok-2"})
        assert pump.call_count == 2
        assert m.token == "tok-2"


class TestPump:
    def test_pump_updates_env_and_clis(self):
        from obo_auth import OBOTokenManager
        m = OBOTokenManager()
        with mock.patch("obo_auth.update_cli_tokens") as upd, \
             mock.patch.object(m, "_write_databrickscfg") as wcfg:
            m._pump("tok-x")
        assert os.environ["DATABRICKS_TOKEN"] == "tok-x"
        upd.assert_called_once_with("tok-x")
        wcfg.assert_called_once_with("tok-x")


class TestState:
    def test_has_token_false_initially(self):
        from obo_auth import OBOTokenManager
        assert OBOTokenManager().has_token is False

    def test_has_token_true_after_capture(self):
        from obo_auth import OBOTokenManager
        m = OBOTokenManager()
        with mock.patch.object(m, "_pump"):
            m.update_from_headers({"x-forwarded-access-token": "t"})
        assert m.has_token is True
```

**Step 2: Run — verify fail**

Run: `uv run pytest tests/test_obo_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'obo_auth'`

**Step 3: Implement**

```python
# obo_auth.py
"""Capture the attendee's OBO user token and pump it into agent CLI configs.

In OBO mode (CODA_OBO_ENABLED, lab only) the app reads x-forwarded-access-token off each
authenticated inbound request and writes it as the agent bearer using the same
pipeline PATRotator uses. The agent then acts AS the attendee. The token is
short-lived (~60 min) and only refreshes on new inbound requests, so a browser
keepalive (see Task 4) keeps it fresh while the tab is open.
"""
import os
import threading
import logging

from utils import ensure_https
from cli_auth import update_cli_tokens

logger = logging.getLogger(__name__)

HEADER = "x-forwarded-access-token"


class OBOTokenManager:
    def __init__(self, host=None):
        self._host = ensure_https(host or os.environ.get("DATABRICKS_HOST", ""))
        self._token = None
        self._lock = threading.Lock()
        self._databrickscfg_path = os.path.join(
            os.environ.get("HOME", "/app/python/source_code"), ".databrickscfg"
        )

    @property
    def token(self):
        with self._lock:
            return self._token

    @property
    def has_token(self):
        return self.token is not None

    def update_from_headers(self, headers):
        """Capture a forwarded token. Returns True if it changed (and was pumped)."""
        token = None
        try:
            token = headers.get(HEADER)
        except Exception:
            token = None
        if not token:
            return False
        token = token.strip()
        with self._lock:
            if token == self._token:
                return False
            self._token = token
        self._pump(token)
        return True

    def _pump(self, token):
        os.environ["DATABRICKS_TOKEN"] = token
        self._write_databrickscfg(token)
        update_cli_tokens(token)
        logger.info("OBO token captured/refreshed: all CLIs updated")

    def _write_databrickscfg(self, token):
        content = f"[DEFAULT]\nhost = {self._host}\ntoken = {token}\n"
        try:
            with open(self._databrickscfg_path, "w") as f:
                f.write(content)
            os.chmod(self._databrickscfg_path, 0o600)
        except OSError as e:
            logger.warning(f"Could not write .databrickscfg: {e}")
```

> Note: tokens are never logged (only "captured/refreshed"), per the auth doc's best practice.

**Step 4: Run — verify pass**

Run: `uv run pytest tests/test_obo_auth.py -v` → all PASS

**Step 5: Commit**

```bash
git add obo_auth.py tests/test_obo_auth.py
git commit -m "feat: add OBOTokenManager (capture + pump forwarded user token)"
```

---

### Task 3: Capture the token on inbound requests + first-capture setup trigger

**Files:**
- Modify: `app.py` — module instance (~`app.py:72`), `authorize_request()` (`app.py:1045`), `handle_ws_connect()` (`app.py:773`); extract `_maybe_trigger_setup()` near `run_setup` (`app.py:489`) and reuse it in `configure_pat` (`app.py:1266-1270`).
- Test: `tests/test_obo_capture.py` (create)

**Step 1: Write the failing test**

```python
# tests/test_obo_capture.py
"""obo mode captures the forwarded token on requests and starts setup once."""
import importlib, os
from unittest import mock


def _reload_obo_app():
    # OBO engages because CODA_PROFILE=lab and the gate is on by default.
    with mock.patch.dict(os.environ, {"CODA_PROFILE": "lab",
                                      "DATABRICKS_HOST": "https://h",
                                      "DATABRICKS_APP_PORT": "8000"}, clear=False):
        os.environ.pop("CODA_OBO_ENABLED", None)
        import app
        return importlib.reload(app)


def test_capture_helper_pumps_and_triggers_setup_first_time():
    app = _reload_obo_app()
    with mock.patch.object(app.obo_manager, "update_from_headers", return_value=True), \
         mock.patch.object(app, "_maybe_trigger_setup") as trig:
        app._capture_obo({"x-forwarded-access-token": "t1"})
        trig.assert_called_once()


def test_capture_no_change_does_not_trigger_setup():
    app = _reload_obo_app()
    with mock.patch.object(app.obo_manager, "update_from_headers", return_value=False), \
         mock.patch.object(app, "_maybe_trigger_setup") as trig:
        app._capture_obo({"x-forwarded-access-token": "t1"})
        trig.assert_not_called()


def test_capture_noop_in_pat_mode():
    # Gate off within lab → pat mode → capture is a no-op.
    with mock.patch.dict(os.environ, {"CODA_PROFILE": "lab", "CODA_OBO_ENABLED": "false",
                                      "DATABRICKS_APP_PORT": "8000"}, clear=False):
        import app
        app = importlib.reload(app)
        with mock.patch.object(app.obo_manager, "update_from_headers") as upd:
            app._capture_obo({"x-forwarded-access-token": "t1"})
            upd.assert_not_called()
```

**Step 2: Run — verify fail**

Run: `uv run pytest tests/test_obo_capture.py -v`
Expected: FAIL — missing `obo_manager` / `_capture_obo` / `_maybe_trigger_setup`.

**Step 3: Implement**

Add import + instance near `pat_rotator` (`app.py:72`):
```python
from obo_auth import OBOTokenManager
obo_manager = OBOTokenManager()
```

Extract the setup trigger (replace the inline block at `app.py:1266-1270` with a call to this, and define near `run_setup` `app.py:489`):
```python
def _maybe_trigger_setup():
    """Start run_setup() in the background if it hasn't completed. Idempotent."""
    with setup_lock:
        if setup_state["status"] != "complete":
            threading.Thread(target=run_setup, daemon=True, name="setup-thread").start()
            logger.info("Setup triggered")
            return True
    return False


def _capture_obo(headers):
    """In obo mode: capture forwarded token; trigger setup on first capture."""
    if _agent_auth_mode() != "obo":
        return
    if obo_manager.update_from_headers(headers):
        _maybe_trigger_setup()
```

Call `_capture_obo(request.headers)` inside `authorize_request()` (`app.py:1045`) — after the authorization check passes, before returning `None`:
```python
    _capture_obo(request.headers)
    return None
```

Call it on WebSocket connect inside `handle_ws_connect()` (`app.py:773`) — after `_check_ws_authorization()` passes. Use the connect-time headers:
```python
    from flask import request as _rq
    _capture_obo(_rq.headers)
```

> Why both: the first page load (HTTP) usually captures the token before any WS connect, but the WS handshake is a reliable second capture point. Both are idempotent (dedupe in `update_from_headers`).

**Step 4: Run — verify pass**

Run: `uv run pytest tests/test_obo_capture.py tests/test_agent_auth_mode.py -v` → all PASS

**Step 5: Commit**

```bash
git add app.py tests/test_obo_capture.py
git commit -m "feat: capture OBO token on HTTP/WS requests, trigger setup on first capture"
```

---

### Task 4: Keepalive refresh endpoint + browser timer

**Files:**
- Modify: `app.py` — add `GET /api/obo-refresh` (near `pat_status` `app.py:1167`)
- Modify: `static/index.html` — add a periodic fetch (find the existing heartbeat/polling JS)
- Test: `tests/test_obo_refresh.py` (create)

**Step 1: Write the failing test**

```python
# tests/test_obo_refresh.py
"""The keepalive endpoint re-captures the forwarded token."""
import importlib, os
from unittest import mock
import pytest


@pytest.fixture
def obo_app():
    # OBO engages because CODA_PROFILE=lab and the gate is on by default.
    with mock.patch.dict(os.environ, {"CODA_PROFILE": "lab",
                                      "DATABRICKS_HOST": "https://h",
                                      "DATABRICKS_APP_PORT": "8000"}, clear=False):
        os.environ.pop("CODA_OBO_ENABLED", None)
        import app
        app = importlib.reload(app)
        app.app.config["TESTING"] = True
        yield app


def test_refresh_recaptures(obo_app):
    with mock.patch.object(obo_app, "_capture_obo") as cap:
        client = obo_app.app.test_client()
        resp = client.get("/api/obo-refresh", headers={"x-forwarded-access-token": "t9"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    cap.assert_called_once()
```

**Step 2: Run — verify fail**

Run: `uv run pytest tests/test_obo_refresh.py -v`

**Step 3: Implement**

Add the endpoint (and whitelist it in `authorize_request`'s skip list at `app.py:1049` so it can't 403 a keepalive, but it must still capture — so capture explicitly):
```python
@app.route("/api/obo-refresh")
def obo_refresh():
    """Keepalive: re-capture the forwarded user token to keep agents authed.

    The browser hits this every ~20 min; each request carries a fresh
    x-forwarded-access-token which we pump into the agent configs.
    """
    _capture_obo(request.headers)
    return jsonify({"ok": True})
```

In `static/index.html`, alongside the existing heartbeat logic, add (only meaningful in obo mode — harmless otherwise):
```javascript
// OBO keepalive: refresh the forwarded user token well under its ~60-min TTL.
setInterval(() => {
  fetch('/api/obo-refresh', { credentials: 'same-origin' }).catch(() => {});
}, 20 * 60 * 1000);
```

> The WebSocket heartbeat can't carry a fresh token (WS headers are fixed at handshake), so this must be an HTTP fetch.

**Step 4: Run — verify pass**

Run: `uv run pytest tests/test_obo_refresh.py -v` → PASS

**Step 5: Commit**

```bash
git add app.py static/index.html tests/test_obo_refresh.py
git commit -m "feat: OBO keepalive endpoint + browser timer (refresh forwarded token)"
```

---

### Task 5: `pat-status` / `configure-pat` awareness (OBO with PAT fallback)

**Files:**
- Modify: `app.py` — `pat_status()` (`app.py:1167`), `configure_pat()` (`app.py:1192`)
- Test: `tests/test_obo_endpoints.py` (create)

**Step 1: Write the failing test**

```python
# tests/test_obo_endpoints.py
"""pat-status reflects OBO token; PAT prompt is fallback, not blocked."""
import importlib, os
from unittest import mock
import pytest


@pytest.fixture
def obo_app():
    # OBO engages because CODA_PROFILE=lab and the gate is on by default.
    with mock.patch.dict(os.environ, {"CODA_PROFILE": "lab",
                                      "DATABRICKS_HOST": "https://h",
                                      "DATABRICKS_APP_PORT": "8000"}, clear=False):
        os.environ.pop("CODA_OBO_ENABLED", None)
        import app
        app = importlib.reload(app)
        app.app.config["TESTING"] = True
        yield app


def test_pat_status_configured_when_obo_token_present(obo_app):
    with mock.patch.object(type(obo_app.obo_manager), "has_token",
                           new_callable=mock.PropertyMock, return_value=True):
        resp = obo_app.app.test_client().get("/api/pat-status")
    body = resp.get_json()
    assert body["configured"] is True and body["valid"] is True


def test_pat_status_not_configured_when_no_obo_token(obo_app):
    with mock.patch.object(type(obo_app.obo_manager), "has_token",
                           new_callable=mock.PropertyMock, return_value=False):
        resp = obo_app.app.test_client().get("/api/pat-status")
    assert resp.get_json()["configured"] is False  # → frontend shows PAT fallback


def test_configure_pat_still_allowed_in_obo(obo_app):
    # OBO mode must NOT hard-reject configure-pat — it's the fallback path.
    with mock.patch.object(obo_app, "_is_databricks_apps", return_value=False):
        resp = obo_app.app.test_client().post("/api/configure-pat", json={"token": ""})
    assert resp.status_code != 409  # empty token → 400, but not a mode rejection
```

**Step 2: Run — verify fail**

Run: `uv run pytest tests/test_obo_endpoints.py -v`

**Step 3: Implement**

At the top of `pat_status()` (`app.py:1169`):
```python
    if _agent_auth_mode() == "obo" and obo_manager.has_token:
        return jsonify({"configured": True, "valid": True, "user": get_request_user() or "user"})
    # else: fall through to PAT logic (PAT fallback path; prompt shown if no PAT)
```

Leave `configure_pat()` working in obo mode (it is the documented fallback for unattended runs). Do **not** add an obo rejection. (No code change beyond confirming the test passes.)

**Step 4: Run — verify pass**

Run: `uv run pytest tests/test_obo_endpoints.py -v` → PASS

**Step 5: Commit**

```bash
git add app.py tests/test_obo_endpoints.py
git commit -m "feat: pat-status reflects OBO token; keep PAT as fallback in obo mode"
```

---

### Task 6: Boot wiring in `initialize_app` + integration smoke test (validates R1)

**Files:**
- Modify: `app.py` — `initialize_app()` (`app.py:1531`)
- Test: `tests/test_obo_boot.py` (create)

**Step 1: Write the failing test**

```python
# tests/test_obo_boot.py
"""obo mode does not require a PAT at boot; setup waits for first capture."""
import importlib, os
from unittest import mock
import pytest


@pytest.fixture
def obo_app():
    # OBO engages because CODA_PROFILE=lab and the gate is on by default.
    with mock.patch.dict(os.environ, {"CODA_PROFILE": "lab",
                                      "DATABRICKS_HOST": "https://h",
                                      "DATABRICKS_APP_PORT": "8000"}, clear=False):
        os.environ.pop("CODA_OBO_ENABLED", None)
        import app
        yield importlib.reload(app)


def test_initialize_does_not_block_on_pat_in_obo(obo_app):
    with mock.patch.object(obo_app, "_is_databricks_apps", return_value=True), \
         mock.patch.object(obo_app, "run_setup") as run_setup:
        obo_app.initialize_app()
        # In obo mode, setup is NOT kicked off at boot; it waits for token capture.
        run_setup.assert_not_called()


def test_initialize_pat_mode_unchanged(obo_app):
    # Regression guard: disabling the gate within lab keeps existing PAT boot behavior.
    with mock.patch.dict(os.environ, {"CODA_PROFILE": "lab", "CODA_OBO_ENABLED": "false"}):
        app2 = importlib.reload(obo_app)
        assert app2._agent_auth_mode() == "pat"
```

**Step 2: Run — verify fail**

Run: `uv run pytest tests/test_obo_boot.py -v`

**Step 3: Implement**

In `initialize_app()` (`app.py:1531`), branch on mode. In `obo` mode: keep stripping SP creds (`app.py:1551-1555` — agents must not silently use the SP), resolve owner from forwarded identity as today, and **do not** require/await a PAT — setup is triggered by `_capture_obo` on the first request. Guard any existing "await PAT before setup" logic with `if _agent_auth_mode() == "pat":`.

```python
    mode = _agent_auth_mode()
    logger.info(f"Agent auth mode: {mode}")
    # ... existing SP-cred strip stays (applies to both modes) ...
    if mode == "pat":
        # existing behavior: wait for configure-pat to trigger setup
        ...
    else:  # obo
        logger.info("OBO mode: setup will start on first forwarded-token capture")
        # do not trigger run_setup here
```

**Step 4: Run — verify pass + full suite**

Run: `uv run pytest tests/test_obo_boot.py tests/test_obo_capture.py tests/test_obo_auth.py tests/test_obo_endpoints.py tests/test_obo_refresh.py tests/test_agent_auth_mode.py -v` → all PASS
Run: `uv run pytest -q` → no regressions

**Step 5: Manual smoke test (validates R1 — CLIs pick up swapped token)**

On a lab workspace with OBO enabled (Task 7):
1. Deploy with `CODA_PROFILE=lab` (OBO gate on by default); open the app (no PAT prompt should appear).
2. Confirm setup runs and an agent (e.g. Claude) can run `databricks current-user me` → returns the **attendee's** identity, not the app SP.
3. Let it idle past ~20 min (one keepalive cycle), then run another agent command → still authed.
4. Have the agent create a trivial resource (e.g. a notebook) → owner is the attendee.

Document results in the PR description.

**Step 6: Commit**

```bash
git add app.py tests/test_obo_boot.py
git commit -m "feat: wire OBO mode into boot (no PAT required; setup on token capture)"
```

---

### Task 7: Headless provisioning — `lab_deploy.py` + Control Tower contract

**Files:**
- Modify: `scripts/lab_deploy.py` (`scripts/lab_deploy.py:52-55` env defaults; the `apps.create`/`App(...)` construction; add the workspace setting patch)
- Modify: `app.yaml.template` (document `CODA_OBO_ENABLED`)
- Test: `tests/test_lab_deploy_obo.py` (create)

**Step 1: Write the failing test**

```python
# tests/test_lab_deploy_obo.py
"""lab_deploy enables the OBO gate and provisions OBO scopes headlessly."""
import importlib
from unittest import mock


def test_default_env_enables_obo_gate():
    import scripts.lab_deploy as ld
    importlib.reload(ld)
    env = ld.default_lab_env()  # extract existing inline env dict into this helper
    assert env["CODA_OBO_ENABLED"] == "true"  # explicit, though it's the default
    assert env["CODA_AUTH_MODE"] == "workspace"
    assert env["CODA_PROFILE"] == "lab"


def test_app_created_with_user_api_scopes():
    import scripts.lab_deploy as ld
    importlib.reload(ld)
    with mock.patch.object(ld, "WorkspaceClient") as WC:
        w = WC.return_value
        ld.enable_obo_and_create_app(w, app_name="lab-x")
    # workspace-level enablement
    w.workspace_settings_v2.patch_public_workspace_setting.assert_called_once()
    # per-app scope declaration
    app_arg = w.apps.create.call_args.kwargs.get("app") or w.apps.create.call_args.args[0]
    assert app_arg.user_api_scopes == ["all-apis"]
```

**Step 2: Run — verify fail**

Run: `uv run pytest tests/test_lab_deploy_obo.py -v`

**Step 3: Implement**

Add `CODA_OBO_ENABLED=true` to the lab env defaults (`scripts/lab_deploy.py:52-55`) — OBO is on by default in lab, but setting it explicitly documents intent and makes it easy to flip off. Extract the env dict into `default_lab_env()` for testability. Add helper:

```python
from databricks.sdk.service.apps import App
from databricks.sdk.service import settingsv2

def enable_obo_and_create_app(w, app_name, **app_kwargs):
    """Headlessly enable OBO (all-apis) for the workspace and create the app
    with user_api_scopes. Order matters: enable the setting BEFORE create."""
    setting = settingsv2.Setting(
        name="allowed_apps_user_api_scopes",
        allowed_apps_user_api_scopes=settingsv2.AllowedAppsUserApiScopesMessage(
            allowed_scopes=["all-apis"],
        ),
    )
    w.workspace_settings_v2.patch_public_workspace_setting(
        name="allowed_apps_user_api_scopes", setting=setting,
    )
    return w.apps.create(
        app=App(name=app_name, user_api_scopes=["all-apis"], **app_kwargs)
    )
```

> Verify the exact `patch_public_workspace_setting` signature/field name against the installed SDK during implementation:
> `uv run python -c "from databricks.sdk import WorkspaceClient; import inspect; print(inspect.signature(WorkspaceClient.workspace_settings_v2.fget.__annotations__))"` — or read `WorkspaceSettingsV2API.patch_public_workspace_setting` source. Adjust `name`/`setting` kwargs to match.

**Control Tower contract (document, no code here):** CT's per-attendee deploy becomes:
1. `patch_public_workspace_setting(allowed_apps_user_api_scopes=["all-apis"])` on the attendee workspace.
2. `apps.create(App(name, user_api_scopes=["all-apis"], ...))`.
3. `apps.deploy` + existing `apps.update_permissions`.
4. Set env `CODA_AUTH_MODE=workspace`, `CODA_PROFILE=lab` (OBO gate on by default; pass `CODA_OBO_ENABLED=false` to opt out).

**Step 4: Run — verify pass**

Run: `uv run pytest tests/test_lab_deploy_obo.py -v` → PASS

**Step 5: Commit**

```bash
git add scripts/lab_deploy.py app.yaml.template tests/test_lab_deploy_obo.py
git commit -m "feat: lab_deploy enables OBO gate and provisions all-apis scope headlessly"
```

---

### Task 8: Docs + the residual preview-gate validation

**Files:**
- Modify: `docs/lab-build.md`

**Step 1: Document** in `docs/lab-build.md`:
- The new `CODA_OBO_ENABLED` gate (on by default, lab-only; set `false` to fall back to PAT), and what each path means for **identity** (obo → attendee owns their work via forwarded token; pat → also attendee but via pasted token). Note OBO is ignored outside `CODA_PROFILE=lab`.
- The OBO runtime model: token captured from `x-forwarded-access-token`, refreshed by the browser keepalive (~20 min), ~60-min TTL.
- **R2 caveat**: closing the tab during a long unattended run can let the token lapse; tell attendees to keep the tab open, or paste a PAT (fallback) for unattended work.
- The provisioning prerequisites (Task 7) + the **residual preview-gate check** below.

**Step 2: Residual preview-gate validation (manual, one-time per account)**

Before relying on headless enablement at scale, confirm on ONE real attendee workspace whether `patch_public_workspace_setting(allowed_apps_user_api_scopes=["all-apis"])` is sufficient, or whether the **account-level "On-Behalf-Of User Authorization" Previews toggle** must also be on first. If the account toggle is required, it's a **one-time account action** (account previews apply to all workspaces) — document it as a prerequisite, not a per-attendee step. Record the finding in `docs/lab-build.md`.

**Step 3: Commit**

```bash
git add docs/lab-build.md
git commit -m "docs: document OBO agent auth, refresh model, and provisioning prerequisites"
```

---

## Definition of Done

- [ ] Dedicated `CODA_OBO_ENABLED` gate (on by default), hard-gated to `CODA_PROFILE=lab` (ignored in full profile); `_agent_auth_mode()` derives `obo`/`pat` from it — Task 1
- [ ] `OBOTokenManager` captures, dedupes, and pumps the forwarded token via the existing CLI pipeline — Task 2
- [ ] Token captured on HTTP `before_request` and WS connect; setup triggered on first capture — Task 3
- [ ] Keepalive endpoint + browser timer keep the ~60-min token fresh — Task 4
- [ ] `pat-status` reflects the OBO token; PAT remains available as fallback — Task 5
- [ ] Boot doesn't require a PAT in obo mode; SP creds still stripped; smoke test confirms agent acts as attendee and survives a keepalive cycle (R1) — Task 6
- [ ] `lab_deploy.py` defaults to obo and provisions `all-apis` scope headlessly; CT contract documented — Task 7
- [ ] Docs updated; residual preview-gate validated and recorded — Task 8
- [ ] `uv run pytest -q` green; no regressions to PAT mode

## Out of scope
- Admin pre-consent automation (attendee one-time "Allow" click accepted).
- Multi-user-per-instance token handling (lab is one workspace per attendee).
- Changing the full (non-lab) profile default away from `pat`.


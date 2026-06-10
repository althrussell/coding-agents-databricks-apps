"""End-to-end proof of the lab deploy contract: workspace-mode auth + lean
profile, deployed the way Control Tower deploys it (SDK path, no source
patching).

Two things are verified here, both against REAL infrastructure (no stubs):

1. ``test_nonowner_reaches_app_in_workspace_mode`` — opens a workspace-mode
   CoDA deployment as a DIFFERENT Databricks identity than the app owner and
   asserts the attendee is admitted (would be a 403 "This app belongs to ..."
   in owner mode). This is the core promise of ``CODA_AUTH_MODE=workspace``:
   any authenticated workspace user gets in, so Control Tower never has to
   patch the app's authorization.

2. ``test_lab_deploy_acceptance`` — the acceptance gate. Runs
   ``scripts/lab_deploy.py`` against a real lab workspace exactly as Control
   Tower would, then confirms the app reaches ACTIVE. Gated behind
   ``CODA_RUN_LAB_DEPLOY=1`` (+ ``CODA_LAB_PROFILE``) because it provisions
   real infrastructure.

Prerequisites are documented in tests/e2e/README.md. Everything skips cleanly
when its inputs are absent, so this module is safe to leave in the default
suite.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Markers that mean the auth wall rejected the caller.
_FORBIDDEN_MARKERS = ("This app belongs to", '"error": "Unauthorized"', '"error":"Unauthorized"')
# Markers that mean we reached a usable CoDA startup state (admitted).
_ADMITTED_MARKERS = ("Token:", "active sessions", "Ready", "? for shortcuts")


def test_nonowner_reaches_app_in_workspace_mode(nonowner_context, lab_app_url):
    """A non-owner attendee must be ADMITTED to a workspace-mode deployment.

    In owner mode this same identity would receive a 403 with
    "This app belongs to <owner>". In workspace mode they get the normal
    landing page (PAT prompt / terminal), proving the auth refactor works
    without any per-attendee source patching.
    """
    page = nonowner_context.new_page()
    page.goto(lab_app_url, timeout=30_000)

    # Wait until either an admitted startup state OR a forbidden marker renders.
    page.wait_for_function(
        """(markers) => {
            const t = document.body.innerText;
            return markers.admitted.some(m => t.includes(m))
                || markers.forbidden.some(m => t.includes(m))
                || /\\$\\s*$/m.test(t);
        }""",
        arg={"admitted": list(_ADMITTED_MARKERS), "forbidden": list(_FORBIDDEN_MARKERS)},
        timeout=90_000,
    )

    body = page.evaluate("() => document.body.innerText")

    forbidden_hit = next((m for m in _FORBIDDEN_MARKERS if m in body), None)
    assert forbidden_hit is None, (
        "Non-owner was BLOCKED by the auth wall in workspace mode "
        f"(matched {forbidden_hit!r}). The app is not running with "
        f"CODA_AUTH_MODE=workspace. Body head:\n{body[:600]}"
    )

    admitted = any(m in body for m in _ADMITTED_MARKERS) or body.strip().endswith("$")
    assert admitted, (
        "Non-owner reached neither a forbidden page nor a known CoDA startup "
        f"state — cannot confirm admission. Body head:\n{body[:600]}"
    )


def test_nonowner_pat_entry_starts_session(nonowner_context, lab_app_url, fresh_pat_nonowner):
    """Full attendee flow: PAT entry then a working bash session.

    Heavier than the admission check (mints a PAT, waits for container setup),
    so it's gated behind CODA_E2E_FULL=1 to keep the default run fast.
    """
    if os.environ.get("CODA_E2E_FULL") != "1":
        pytest.skip("set CODA_E2E_FULL=1 to run the full PAT-entry attendee flow")

    page = nonowner_context.new_page()
    page.goto(lab_app_url, timeout=30_000)
    page.wait_for_function(
        """() => {
            const t = document.body.innerText;
            return t.includes('Token:') || t.includes('active sessions')
                || t.includes('Ready') || /\\$\\s*$/m.test(t);
        }""",
        timeout=90_000,
    )

    body = page.evaluate("() => document.body.innerText")
    if "Token:" in body:
        page.get_by_role("textbox", name="Terminal input").fill(fresh_pat_nonowner + "\n")
        page.wait_for_function(
            """() => document.body.innerText.includes('Ready')""",
            timeout=180_000,
        )

    # Create a bash session via the same endpoint the UI uses and run a command.
    new_session = page.evaluate(
        """async () => {
            const r = await fetch('/api/session', {
                method: 'POST', credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({label: 'e2e-nonowner'}),
            });
            return r.json();
        }"""
    )
    sid = new_session["session_id"]
    time.sleep(2)

    marker = "CODA-NONOWNER-OK"
    page.evaluate(
        """async ({sid, cmd}) => {
            await fetch('/api/input', {
                method: 'POST', credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({session_id: sid, input: cmd + '\\n'}),
            });
        }""",
        {"sid": sid, "cmd": f"echo {marker}"},
    )

    deadline = time.time() + 30
    accumulated = ""
    while time.time() < deadline:
        chunk = page.evaluate(
            """async ({sid}) => {
                const r = await fetch('/api/output', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({session_id: sid}),
                });
                return (await r.json()).output || '';
            }""",
            {"sid": sid},
        )
        accumulated += chunk
        if marker in accumulated and accumulated.count(marker) >= 2:
            break  # echo of the command + the command's output
        time.sleep(1)

    assert marker in accumulated, (
        f"non-owner bash session never echoed {marker!r}. Output:\n{accumulated[-1500:]}"
    )


def test_lab_deploy_acceptance():
    """Acceptance gate: run scripts/lab_deploy.py against a real lab workspace.

    This exercises the EXACT idempotent SDK path Control Tower uses
    (repos.create -> apps.create_and_wait -> apps.deploy_and_wait) and then
    confirms the app reaches ACTIVE. It provisions real infrastructure, so it
    only runs when explicitly enabled.
    """
    if os.environ.get("CODA_RUN_LAB_DEPLOY") != "1":
        pytest.skip("set CODA_RUN_LAB_DEPLOY=1 (+ CODA_LAB_PROFILE) to run the deploy gate")

    profile = os.environ.get("CODA_LAB_PROFILE", "").strip()
    if not profile:
        pytest.skip("set CODA_LAB_PROFILE to the real lab workspace profile")

    app_name = os.environ.get("CODA_LAB_APP_NAME", "coda-lab")

    # Run the deploy script exactly as `make lab-deploy` would.
    proc = subprocess.run(
        [
            "uv", "run", "python", "scripts/lab_deploy.py",
            "--profile", profile, "--app-name", app_name,
        ],
        cwd=str(REPO_ROOT),
        capture_output=True, text=True,
        timeout=30 * 60,  # create_and_wait + deploy_and_wait can be slow
    )
    print("\n=== lab_deploy.py stdout (tail) ===")
    print(proc.stdout[-4000:])
    if proc.returncode != 0:
        print("=== lab_deploy.py stderr (tail) ===")
        print(proc.stderr[-4000:])
    assert proc.returncode == 0, f"lab_deploy.py exited {proc.returncode}"

    # Confirm the app is ACTIVE (idempotent re-run would also be fine).
    get = subprocess.run(
        ["databricks", "apps", "get", app_name, "--profile", profile, "--output", "json"],
        capture_output=True, text=True, timeout=30,
    )
    assert get.returncode == 0, f"apps get failed: {get.stderr.strip()}"
    state = json.loads(get.stdout).get("compute_status", {}).get("state", "")
    assert state in ("ACTIVE", "RUNNING"), f"lab app not active (state={state!r})"

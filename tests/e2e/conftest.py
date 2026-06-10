"""Playwright e2e fixtures for the live CoDA app.

Tests in this directory drive a real deployed CoDA instance — they exist
because Docker can't replicate the Databricks Apps SSO + PAT-rotator
flow. Each test:

1. Loads pre-recorded SSO auth state (`auth.json`) so the browser starts
   already logged in to Databricks. See README for how to record it.
2. Mints a fresh PAT via the Databricks CLI.
3. Drives the app via Playwright + the same /api/input + DOM-scrape
   pattern the chrome-devtools MCP session used.

Token cost per run: zero LLM tokens. Wall time: ~1-2 min per test.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AUTH_STATE = Path(__file__).parent / "auth.json"
# Second recorded SSO session for a NON-OWNER attendee identity, used to prove
# CODA_AUTH_MODE=workspace lets any authenticated workspace user in. Record it
# with `make e2e-auth-nonowner` (see tests/e2e/README.md).
NONOWNER_AUTH_STATE = Path(__file__).parent / "auth_nonowner.json"


def _databricks_profile() -> str:
    return os.environ.get("DATABRICKS_PROFILE", "daveok")


def _nonowner_profile() -> str:
    """CLI profile for the non-owner attendee (mints that user's PATs)."""
    return os.environ.get("CODA_NONOWNER_PROFILE", "").strip()


def _lab_profile() -> str:
    """CLI profile for the real lab workspace used by the acceptance gate."""
    return os.environ.get("CODA_LAB_PROFILE", "").strip()


def _lab_app_url() -> str:
    """Resolve the workspace-mode lab app URL.

    Prefers the explicit ``CODA_LAB_APP_URL`` override; otherwise resolves the
    app named by ``CODA_LAB_APP_NAME`` (default ``coda-lab``) via the lab
    profile. Skips cleanly when neither is available.
    """
    override = os.environ.get("CODA_LAB_APP_URL", "").strip()
    if override:
        return override
    profile = _lab_profile()
    if not profile:
        pytest.skip(
            "no lab app URL — set CODA_LAB_APP_URL or CODA_LAB_PROFILE "
            "(+ optional CODA_LAB_APP_NAME)"
        )
    app_name = os.environ.get("CODA_LAB_APP_NAME", "coda-lab")
    result = subprocess.run(
        ["databricks", "apps", "get", app_name, "--profile", profile, "--output", "json"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot resolve lab app URL: {result.stderr.strip()}")
    import json
    return json.loads(result.stdout)["url"]


def _app_url() -> str:
    """Resolve the CoDA app URL for the configured profile.

    Reads `databricks apps get coding-agents --profile <profile>` so the
    test doesn't have to hardcode workspace URLs.
    """
    override = os.environ.get("CODA_APP_URL", "").strip()
    if override:
        return override
    result = subprocess.run(
        [
            "databricks", "apps", "get", "coding-agents",
            "--profile", _databricks_profile(), "--output", "json",
        ],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(
            f"Cannot resolve app URL (databricks apps get failed): "
            f"{result.stderr.strip()}"
        )
    import json
    return json.loads(result.stdout)["url"]


def _mint_pat() -> str:
    """Mint a short-lived PAT via the Databricks CLI for the test session."""
    result = subprocess.run(
        [
            "databricks", "tokens", "create",
            "--lifetime-seconds", "3600",   # 1h — comfortably covers the test
            "--comment", "coda-e2e-test",
            "--profile", _databricks_profile(),
            "--output", "json",
        ],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        pytest.skip(
            f"Cannot mint PAT (databricks tokens create failed): "
            f"{result.stderr.strip()}"
        )
    import json
    return json.loads(result.stdout)["token_value"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items):
    """Skip owner-flow e2e tests if the owner SSO prerequisites aren't met.

    Only items that request the owner ``app_url`` fixture are gated here.
    Non-owner tests (``lab_app_url`` / ``nonowner_context``) and the
    lab-deploy acceptance gate self-guard via their own fixtures so a missing
    owner ``auth.json`` doesn't mask them.
    """
    skips = []
    if not AUTH_STATE.exists():
        skips.append(
            f"missing {AUTH_STATE.relative_to(REPO_ROOT)} — "
            f"run `make e2e-auth` first to record SSO session"
        )
    if subprocess.run(
        ["databricks", "current-user", "me", "--profile", _databricks_profile()],
        capture_output=True, timeout=10,
    ).returncode != 0:
        skips.append(
            f"databricks CLI not authed for profile {_databricks_profile()!r} — "
            f"run `databricks auth login --profile {_databricks_profile()}`"
        )
    if skips:
        skip_marker = pytest.mark.skip(reason=" | ".join(skips))
        for item in items:
            if "app_url" in getattr(item, "fixturenames", ()):
                item.add_marker(skip_marker)


@pytest.fixture(scope="module")
def app_url() -> str:
    return _app_url()


@pytest.fixture(scope="module")
def auth_state_path() -> str:
    return str(AUTH_STATE)


@pytest.fixture
def fresh_pat() -> str:
    """A freshly-minted 1h PAT. New token per test to avoid cross-test bleed."""
    return _mint_pat()


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Inject the recorded SSO storage state into every Playwright context."""
    return {**browser_context_args, "storage_state": str(AUTH_STATE)}


# ---------------------------------------------------------------------------
# Non-owner attendee + lab-workspace fixtures (workspace-mode auth proof)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def lab_app_url() -> str:
    return _lab_app_url()


@pytest.fixture
def nonowner_context(browser):
    """A fresh Playwright context authed as the NON-OWNER attendee.

    Skips cleanly if the second SSO state (auth_nonowner.json) hasn't been
    recorded. Yields a context whose pages start logged in as a different
    Databricks identity than the app owner.
    """
    if not NONOWNER_AUTH_STATE.exists():
        pytest.skip(
            f"missing {NONOWNER_AUTH_STATE.name} — run `make e2e-auth-nonowner` "
            f"to record a second (attendee) SSO session"
        )
    context = browser.new_context(storage_state=str(NONOWNER_AUTH_STATE))
    try:
        yield context
    finally:
        context.close()


@pytest.fixture
def fresh_pat_nonowner() -> str:
    """A freshly-minted 1h PAT for the non-owner attendee profile."""
    profile = _nonowner_profile()
    if not profile:
        pytest.skip("set CODA_NONOWNER_PROFILE to mint the attendee's PAT")
    result = subprocess.run(
        [
            "databricks", "tokens", "create",
            "--lifetime-seconds", "3600",
            "--comment", "coda-e2e-nonowner",
            "--profile", profile,
            "--output", "json",
        ],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot mint non-owner PAT: {result.stderr.strip()}")
    import json
    return json.loads(result.stdout)["token_value"]

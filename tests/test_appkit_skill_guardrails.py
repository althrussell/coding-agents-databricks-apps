"""Guardrail tests for the AppKit-default app-build skill.

These are content invariants, not behaviour tests: they fail loudly if a future
edit reverts CoDA to a Streamlit/Delta default, drops the CoDA UX contract, or
removes the starter overlay the agent copies from. They are fast (pure file
reads, no network, no Docker) so they run on every CI invocation.

The asserts intentionally check lowercased substrings / structural markers
rather than exact prose, so harmless wording tweaks don't break them — but the
load-bearing directives (AppKit is the hard default, Streamlit is opt-in only,
the UX defaults exist) cannot be removed without a test failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / ".claude" / "skills" / "databricks-apps-python"
SKILL_MD = SKILL_DIR / "SKILL.md"
FRAMEWORKS_MD = SKILL_DIR / "3-frameworks.md"
UX_MD = SKILL_DIR / "7-appkit-ux.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
LAKEBASE_MD = SKILL_DIR / "5-lakebase.md"
LAB_COACH_MD = REPO_ROOT / "instructions" / "lab_coach.md"
OVERLAY_DIR = SKILL_DIR / "examples" / "appkit-ux"


def _read(path: Path) -> str:
    assert path.exists(), f"expected file missing: {path}"
    return path.read_text()


# ---------------------------------------------------------------------------
# AppKit is the hard default; Streamlit/Delta are opt-in only
# ---------------------------------------------------------------------------


class TestAppKitIsTheHardDefault:
    def test_skill_declares_appkit_lakebase_hard_default(self):
        text = _read(SKILL_MD).lower()
        assert "appkit" in text and "lakebase" in text
        assert "hard default" in text, (
            "SKILL.md must declare AppKit + Lakebase as the HARD default"
        )

    def test_skill_marks_python_frameworks_opt_in(self):
        text = _read(SKILL_MD).lower()
        assert "opt-in" in text, "Python frameworks must be marked opt-in only"

    def test_skill_has_no_streamlit_default(self):
        """Guard against a regression that re-introduces a Streamlit/Python default."""
        text = _read(SKILL_MD).lower()
        # The skill must explicitly state there is no Python default AND must
        # explicitly tell the agent NOT to default to Streamlit.
        assert "there is no python default" in text
        assert "do not default to streamlit" in text, (
            "SKILL.md must explicitly prohibit defaulting to Streamlit"
        )

    def test_frameworks_guide_marks_streamlit_opt_in(self):
        text = _read(FRAMEWORKS_MD).lower()
        assert "appkit" in text
        assert "opt-in" in text, (
            "3-frameworks.md must state Python frameworks are opt-in only"
        )

    def test_claude_md_propagates_appkit_default(self):
        text = _read(CLAUDE_MD).lower()
        assert "appkit" in text and "lakebase" in text
        assert "build" in text and "app" in text, (
            "CLAUDE.md must carry the AppKit-first app-building directive"
        )


# ---------------------------------------------------------------------------
# The CoDA UX contract exists and is enforced
# ---------------------------------------------------------------------------


class TestUxContract:
    def test_ux_guide_exists_with_all_mandatory_defaults(self):
        text = _read(UX_MD).lower()
        # Every always-on UX default must be present.
        for marker in (
            "app shell",
            "theme",
            "light/dark",
            "loading",
            "empty",
            "error",
            "responsive",
            "lucide",
        ):
            assert marker in text, f"7-appkit-ux.md missing UX default: {marker!r}"

    def test_ux_guide_has_app_type_layout_map(self):
        text = _read(UX_MD).lower()
        for app_type in ("dashboard", "crud", "chat", "form"):
            assert app_type in text, f"app-type→layout map missing: {app_type!r}"

    def test_ux_guide_records_pinned_appkit_version_path(self):
        text = _read(UX_MD)
        assert "~/.coda/appkit-version" in text, (
            "UX guide must reference the pinned-version file written at boot"
        )

    def test_skill_links_ux_guide(self):
        assert "7-appkit-ux.md" in _read(SKILL_MD)


# ---------------------------------------------------------------------------
# The starter overlay the agent copies from must exist
# ---------------------------------------------------------------------------


class TestStarterOverlay:
    @pytest.mark.parametrize(
        "filename",
        [
            "README.md",
            "app-shell.tsx",
            "theme-provider.tsx",
            "data-view-states.tsx",
            "dashboard-page.tsx",
        ],
    )
    def test_overlay_file_present(self, filename):
        assert (OVERLAY_DIR / filename).exists(), (
            f"AppKit UX starter overlay missing {filename}"
        )

    def test_overlay_referenced_by_guide(self):
        text = _read(UX_MD)
        for ref in ("app-shell.tsx", "theme-provider.tsx", "data-view-states.tsx"):
            assert ref in text, f"UX guide must reference overlay file {ref}"


# ---------------------------------------------------------------------------
# On-demand, non-interactive Lakebase binding (no UI clicks)
# ---------------------------------------------------------------------------


class TestLakebaseBinding:
    def test_skill_uses_non_interactive_binding(self):
        text = _read(SKILL_MD).lower()
        # The golden path must drive lakebase_ensure.py + non-interactive bind,
        # not an interactive "when prompted, choose..." flow.
        assert "lakebase_ensure.py" in text
        assert "--auto-approve" in text
        assert "~/.coda/lakebase.json" in text
        assert "when prompted" not in text, (
            "SKILL.md must NOT instruct the user to answer interactive prompts"
        )

    def test_lakebase_guide_drops_ui_click_setup(self):
        text = _read(LAKEBASE_MD)
        assert "lakebase_ensure.py" in text
        lower = text.lower()
        assert "on-demand" in lower or "on demand" in lower
        # The old "add Lakebase as an app resource in the Databricks UI" click
        # path must be gone.
        assert "in the databricks ui" not in lower

    def test_helper_script_exists(self):
        assert (REPO_ROOT / "scripts" / "lakebase_ensure.py").exists()

    def test_claude_md_forbids_ui_clicks(self):
        text = _read(CLAUDE_MD).lower()
        assert "lakebase_ensure.py" in text
        assert "never make the user click" in text


# ---------------------------------------------------------------------------
# The guided lab-coach contract exists and is wired
# ---------------------------------------------------------------------------


class TestLabCoachContract:
    def test_claude_md_has_always_on_guided_contract(self):
        text = _read(CLAUDE_MD).lower()
        # Clarify → recommend → confirm, plus the build payoff.
        assert "lead with your recommendation" in text
        assert "confirm" in text
        assert "live app url" in text

    def test_lab_coach_file_exists_with_persona_and_payoff(self):
        text = _read(LAB_COACH_MD)
        lower = text.lower()
        assert "technical" in lower and "business" in lower
        assert "~/.coda/persona" in text, "coach must persist persona"
        assert "live app url" in lower
        assert "start over" in lower, "coach must offer a reset path"

    def test_lab_coach_has_injection_marker(self):
        # app._inject_lab_coach relies on this sentinel for idempotency.
        assert "<!-- coda-lab-coach -->" in _read(LAB_COACH_MD)

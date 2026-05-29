# `coda_run` Workflow Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inject a Databricks orientation block (CAPABILITIES) and a structured 3-phase workflow protocol (PLAN → EXECUTE → SYNTHESIZE with critique at each phase) into every `coda_run` task's `prompt.txt`. Add a third terminal `result.json` status `"info_needed"` with a required `feedback` field so the calling client can iterate when the agent is blocked. Update `coda_inbox`, `coda_get_result`, and the MCP `instructions` block to know about the new status and its `needs_approval` sibling.

**Architecture:**
- Pure-function module `coda_mcp/databricks_preamble.py` produces the two new prompt sections (CAPABILITIES, WORKFLOW PROTOCOL). One source of truth for the skill list. Trivially unit-testable.
- `task_manager.wrap_prompt()` gains a `workflow_protocol: bool = True` parameter. When true, inserts the two sections between TASK and INSTRUCTIONS, and updates INSTRUCTIONS to describe new step labels and the `info_needed` status. The flag flows from `coda_run` through `create_task` to `wrap_prompt` — three call sites, one parameter.
- Inbox / result surfaces (`coda_inbox` counts dict, `coda_get_result` docstring, the FastMCP `instructions=` block at server construction) are updated to tolerate and surface the new statuses (`info_needed`, `needs_approval`).
- Tests pin the prompt sections verbatim where it matters, pin the skill list against CLAUDE.md, and guard the new counts-dict keys and docstring content.

**Tech Stack:** Python 3.11, pytest, MagicMock, FastMCP. No new dependencies.

---

## Files modified by this plan

- **Create:** `coda_mcp/databricks_preamble.py` — new module, three exports
- **Create:** `tests/test_databricks_preamble.py` — unit tests for the new module
- **Modify:** `coda_mcp/task_manager.py:153-225` — `wrap_prompt` signature, body, INSTRUCTIONS section text
- **Modify:** `coda_mcp/task_manager.py:231-...` — `create_task` signature + forwarding
- **Modify:** `coda_mcp/mcp_server.py:52-99` — FastMCP `instructions=` block (add INFO_NEEDED HANDOFF paragraph)
- **Modify:** `coda_mcp/mcp_server.py:220-227` — `coda_run` signature + forwarding
- **Modify:** `coda_mcp/mcp_server.py:551-559` — `coda_inbox` counts dict
- **Modify:** `coda_mcp/mcp_server.py:573-584` — `coda_get_result` docstring
- **Create:** `tests/test_inbox_status_passthrough.py` — counts dict + docstring + MCP instructions tests

## Pre-flight context

- Worktree: `/Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp`
- Branch: `feat/coda-mcp-interactive-handoff` (PR #67, in-flight — this lands as follow-up commits)
- Run tests with `uv run pytest` (per user's `always use uv` directive)
- Commit identity: `-c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty"`. No AI/Claude co-author lines.
- The full spec is `docs/superpowers/specs/2026-05-28-coda-run-workflow-protocol-design.md` — consult for full text of CAPABILITIES, WORKFLOW PROTOCOL, and DISAMBIGUATION sections.
- Skill list source of truth: the "Databricks Skills" markdown table in the project-level `CLAUDE.md` at the repo root (`/Users/sathish.gangichetty/Documents/xterm-experiment/.worktrees/coda-mcp/CLAUDE.md`).

---

## Task 1: Create `databricks_preamble.py` module and unit tests (TDD)

This task creates a new module with three pure functions and exhaustive tests. New module → tests and implementation land in the same commit (the module doesn't exist for the tests to fail against prior to the commit, so "RED-then-GREEN-in-one-commit" is the right shape here).

**Files:**
- Create: `coda_mcp/databricks_preamble.py`
- Create: `tests/test_databricks_preamble.py`

- [ ] **Step 1: Write the new module `coda_mcp/databricks_preamble.py`**

Create the file with this exact content:

```python
"""Builders for the CoDA prompt envelope's CAPABILITIES and WORKFLOW PROTOCOL sections.

These are injected into prompt.txt by ``task_manager.wrap_prompt`` when
``workflow_protocol=True``. Pure functions — no side effects, no I/O.
"""
from __future__ import annotations


_DATABRICKS_SKILLS: tuple[str, ...] = (
    "agent-bricks",
    "databricks-genie",
    "databricks-app-python",
    "databricks-app-apx",
    "databricks-jobs",
    "databricks-unity-catalog",
    "spark-declarative-pipelines",
    "aibi-dashboards",
    "model-serving",
    "mlflow-evaluation",
    "asset-bundles",
    "databricks-python-sdk",
    "databricks-config",
    "databricks-docs",
    "synthetic-data-generation",
    "unstructured-pdf-generation",
)


def get_databricks_skills() -> tuple[str, ...]:
    """Return the canonical Databricks skill list. Tests pin this against CLAUDE.md."""
    return _DATABRICKS_SKILLS


def build_capabilities() -> str:
    """Orientation block: CLI, skills, MCP servers, when to prefer Databricks-native paths."""
    skills_lines = []
    # Pack 4 skills per line for readability in prompt.txt.
    for i in range(0, len(_DATABRICKS_SKILLS), 4):
        chunk = _DATABRICKS_SKILLS[i:i + 4]
        skills_lines.append("- " + ", ".join(chunk))
    skills_block = "\n".join(skills_lines)
    return (
        "You are running inside CoDA on a Databricks-authenticated host.\n"
        "\n"
        "Databricks CLI: pre-configured. `databricks current-user me` confirms auth.\n"
        "Use it for jobs, workspace, clusters, warehouses, Unity Catalog operations.\n"
        "\n"
        "Skills available at ~/.claude/skills/ — read each skill's SKILL.md before\n"
        "invoking. Relevant Databricks skills:\n"
        f"{skills_block}\n"
        "\n"
        "MCP servers wired:\n"
        "- DeepWiki — ask_question, read_wiki_contents for any GitHub repo\n"
        "- Exa — web_search_exa, web_fetch_exa for live web context\n"
        "- CoDA — chain follow-up tasks via previous_session_id\n"
        "\n"
        "When the task touches Databricks data, pipelines, jobs, dashboards, agents,\n"
        "or model serving, DEFAULT to the skill / CLI / SDK path above instead of\n"
        "generic Python or web search."
    )


def build_workflow_protocol() -> str:
    """3-phase workflow with critique at each phase + info_needed escape hatch."""
    return (
        "You MUST process this task in three phases. Emit status.jsonl events as\n"
        "you go (one JSON object per line, format below).\n"
        "\n"
        "PHASE 1 — PLAN\n"
        "- Write a step-by-step plan as a status.jsonl line with step=\"plan\" and\n"
        "  message containing the numbered steps.\n"
        "- Then critique your own plan as if you were a separate reviewer.\n"
        "  (Spawn a sub-agent for the critique if your agent supports it; otherwise\n"
        "  write the critique inline as a self-review.) Emit step=\"critique_plan\"\n"
        "  with the verdict (APPROVE / BLOCK / APPROVE-WITH-FIXES) and findings.\n"
        "- If the critique surfaces blockers, revise the plan once and re-emit\n"
        "  step=\"plan\". Maximum 2 plan iterations total.\n"
        "- If after 2 attempts you still cannot produce a viable plan, write\n"
        "  result.json with status=\"info_needed\" (see below) and stop.\n"
        "\n"
        "PHASE 2 — EXECUTE\n"
        "- Work the plan. Emit step=\"execute_<n>\" lines after completing each plan\n"
        "  step (n is 1-indexed, matches the plan's numbering).\n"
        "- After execution, emit step=\"critique_execute\" with a review of what got\n"
        "  built vs what the plan said. APPROVE / BLOCK / APPROVE-WITH-FIXES.\n"
        "- If the critique surfaces correctness or scope gaps, fix them and re-emit\n"
        "  step=\"critique_execute\". Maximum 2 execute iterations total.\n"
        "- If you hit a hard blocker (missing access, missing data, ambiguous\n"
        "  requirements that the plan revealed only mid-execution), write\n"
        "  result.json with status=\"info_needed\" and stop.\n"
        "\n"
        "PHASE 3 — SYNTHESIZE\n"
        "- Write result.json with status=\"completed\".\n"
        "- Emit step=\"critique_synthesize\" with a review of the result against the\n"
        "  original TASK.\n"
        "- If the critique surfaces gaps, revise result.json. Maximum 2 synthesis\n"
        "  iterations total.\n"
        "\n"
        "If at any phase you cannot proceed, use the INFO_NEEDED escape hatch:\n"
        "- Set status=\"info_needed\" in result.json.\n"
        "- Set \"feedback\" to a precise, actionable string naming exactly what is\n"
        "  missing (a table name, a decision, an access grant, a clarification).\n"
        "  The calling client will read this and resubmit with the missing context.\n"
        "- \"info_needed\" is NOT a failure — it is a structured request for\n"
        "  iteration. Use it whenever you would otherwise have to guess.\n"
        "\n"
        "If you encounter a hard, unrecoverable failure (a command crashed, an SDK\n"
        "returned 500, a file is corrupt), use status=\"failed\" with a description\n"
        "in \"errors\".\n"
        "\n"
        "DISAMBIGUATION — two soft statuses already exist and they mean different\n"
        "things; use the right one:\n"
        "- \"info_needed\" — the CALLER must add missing context (table name,\n"
        "  business decision, file contents, access grant) before the task can\n"
        "  proceed. Used when ambiguity or missing input blocks you.\n"
        "- \"needs_approval\" — you have a concrete plan to do something destructive\n"
        "  (drop a table, delete a job, modify permissions). You will execute it\n"
        "  if and only if the caller explicitly approves. Used at the SAFETY\n"
        "  boundary, never for ambiguity. See SAFETY section below.\n"
        "\n"
        "If both apply (e.g. \"I'd drop a table but I'm not sure which one\"), prefer\n"
        "\"info_needed\" — resolving the ambiguity first is cheaper than approving\n"
        "the wrong destructive action."
    )
```

- [ ] **Step 2: Write `tests/test_databricks_preamble.py`**

Create the file with this exact content:

```python
"""Unit tests for coda_mcp.databricks_preamble."""
import re

from coda_mcp.databricks_preamble import (
    build_capabilities,
    build_workflow_protocol,
    get_databricks_skills,
)


def test_get_databricks_skills_returns_exactly_sixteen():
    skills = get_databricks_skills()
    assert isinstance(skills, tuple)
    assert len(skills) == 16, f"Expected 16 skills, got {len(skills)}: {skills}"


def test_skills_list_matches_claude_md():
    """The hardcoded skill tuple must match the Databricks Skills table in CLAUDE.md.

    Drift in either direction (added to tuple but not docs, or vice versa) fails
    this test. The test is the canary that forces both sources to stay in sync.
    """
    import os
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    claude_md = os.path.join(repo_root, "CLAUDE.md")
    with open(claude_md, "r") as f:
        text = f.read()
    # Find the Databricks Skills section. Names are comma-separated within table cells.
    section_match = re.search(
        r"###\s+Databricks Skills.*?(?=\n###|\n##|\Z)",
        text, re.DOTALL,
    )
    assert section_match, "Could not find 'Databricks Skills' section in CLAUDE.md"
    section = section_match.group(0)
    # Extract skill names: kebab-case tokens that follow a list pattern. Be loose —
    # accept anything that looks like a skill identifier inside table cells.
    skill_names_in_md = set(re.findall(r"\b([a-z][a-z0-9-]{2,}(?:-[a-z0-9]+)+)\b", section))
    skills_in_code = set(get_databricks_skills())
    # Every skill in code must appear in CLAUDE.md.
    missing_from_md = skills_in_code - skill_names_in_md
    assert not missing_from_md, (
        f"Skills in code but NOT in CLAUDE.md (update CLAUDE.md): {missing_from_md}"
    )
    # Every skill in CLAUDE.md's Databricks section must appear in code.
    # Filter out section/category words that match the regex but aren't skill names.
    section_noise = {
        "ai-agents", "data-engineering",  # category labels, hyphenated
    }
    missing_from_code = (skill_names_in_md - skills_in_code) - section_noise
    assert not missing_from_code, (
        f"Skills in CLAUDE.md but NOT in code (update databricks_preamble.py): "
        f"{missing_from_code}"
    )


def test_capabilities_mentions_cli():
    text = build_capabilities()
    assert "Databricks CLI" in text
    assert "databricks current-user me" in text


def test_capabilities_lists_at_least_ten_skills():
    text = build_capabilities()
    skills = get_databricks_skills()
    hits = sum(1 for s in skills if s in text)
    assert hits >= 10, f"Expected at least 10 skills in CAPABILITIES, found {hits}"


def test_capabilities_mentions_all_three_mcp_servers():
    text = build_capabilities()
    assert "DeepWiki" in text
    assert "Exa" in text
    assert "CoDA" in text


def test_capabilities_under_token_budget():
    text = build_capabilities()
    # ~4 chars/token rough lower bound. 1600 chars ≈ 400 tokens budget.
    assert len(text) < 1600, (
        f"CAPABILITIES is {len(text)} chars (~{len(text)//4} tokens); budget is 1600."
    )


def test_workflow_protocol_lists_three_phases():
    text = build_workflow_protocol()
    assert "PHASE 1 — PLAN" in text
    assert "PHASE 2 — EXECUTE" in text
    assert "PHASE 3 — SYNTHESIZE" in text


def test_workflow_protocol_caps_iterations_at_two():
    text = build_workflow_protocol()
    # The string "Maximum 2" should appear once per phase = 3 times.
    count = text.count("Maximum 2")
    assert count == 3, f"Expected 'Maximum 2' to appear 3 times (once per phase); got {count}"


def test_workflow_protocol_describes_info_needed():
    text = build_workflow_protocol()
    assert "info_needed" in text
    assert "feedback" in text


def test_workflow_protocol_disambiguates_needs_approval():
    text = build_workflow_protocol()
    assert "needs_approval" in text
    assert "DISAMBIGUATION" in text


def test_workflow_protocol_under_token_budget():
    text = build_workflow_protocol()
    # ~4 chars/token. 3200 chars ≈ 800 tokens budget.
    assert len(text) < 3200, (
        f"WORKFLOW PROTOCOL is {len(text)} chars (~{len(text)//4} tokens); budget is 3200."
    )
```

- [ ] **Step 3: Run the test file to verify everything passes**

Run: `uv run pytest tests/test_databricks_preamble.py -v`
Expected: 11 passed.

If a test fails, fix the module (NOT the test) — the test pins the spec.

The one possible test that needs adjustment: `test_skills_list_matches_claude_md` reads CLAUDE.md and parses its Databricks Skills section. The regex pattern is loose; if it picks up false-positives (e.g. category labels that contain hyphens), add them to `section_noise`. Don't loosen the assertion itself.

- [ ] **Step 4: Run ruff check**

Run: `uv run ruff check coda_mcp/databricks_preamble.py tests/test_databricks_preamble.py`
Expected: All checks passed.

- [ ] **Step 5: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add coda_mcp/databricks_preamble.py tests/test_databricks_preamble.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat: add databricks_preamble module — CAPABILITIES + WORKFLOW PROTOCOL builders

Two pure-function builders for the new prompt envelope sections plus the
canonical Databricks skill list. Tests pin the skill list against CLAUDE.md
to catch drift in either direction, and pin both sections to token budgets."
```

---

## Task 2: Wire `workflow_protocol` flag through wrap_prompt → create_task → coda_run (TDD)

A single flag, three call sites. TDD: write the tests against the desired flow, watch them fail, then wire the flag.

**Files:**
- Modify: `coda_mcp/task_manager.py:153-225` (`wrap_prompt` — signature + body)
- Modify: `coda_mcp/task_manager.py:231-...` (`create_task` — signature + forward)
- Modify: `coda_mcp/mcp_server.py:220-227` (`coda_run` — signature + forward)
- Modify (or create): `tests/test_task_manager.py` (extend if exists; create otherwise)

- [ ] **Step 1: Check whether `tests/test_task_manager.py` already exists**

Run: `ls -la tests/test_task_manager.py 2>&1 || echo "MISSING"`

If it exists, you'll append tests. If it doesn't, you'll create it.

- [ ] **Step 2: Append (or create with) these tests for the flag wiring**

Add these tests to `tests/test_task_manager.py` (create the file if missing — start with `"""Tests for coda_mcp.task_manager."""` plus imports).

```python
def test_wrap_prompt_default_includes_capabilities_and_workflow():
    """Default workflow_protocol=True; rendered prompt contains both new sections."""
    from coda_mcp.task_manager import wrap_prompt

    out = wrap_prompt(
        task_id="t-1",
        session_id="s-1",
        email="user@example.com",
        prompt="do the thing",
        context=None,
        results_dir="/tmp/results",
    )
    assert "CAPABILITIES:" in out
    assert "WORKFLOW PROTOCOL:" in out
    # Sanity: still has the existing structure.
    assert "TASK:" in out
    assert "INSTRUCTIONS:" in out
    assert "SAFETY:" in out


def test_wrap_prompt_workflow_protocol_false_omits_sections():
    """With workflow_protocol=False, both new sections are absent."""
    from coda_mcp.task_manager import wrap_prompt

    out = wrap_prompt(
        task_id="t-1",
        session_id="s-1",
        email="user@example.com",
        prompt="do the thing",
        context=None,
        results_dir="/tmp/results",
        workflow_protocol=False,
    )
    assert "CAPABILITIES:" not in out
    assert "WORKFLOW PROTOCOL:" not in out
    # Existing sections are still present.
    assert "TASK:" in out
    assert "INSTRUCTIONS:" in out


def test_wrap_prompt_workflow_protocol_default_is_true():
    """Signature inspection: default value of workflow_protocol is True."""
    import inspect
    from coda_mcp.task_manager import wrap_prompt

    sig = inspect.signature(wrap_prompt)
    assert "workflow_protocol" in sig.parameters
    assert sig.parameters["workflow_protocol"].default is True


def test_create_task_signature_has_workflow_protocol_param():
    """create_task accepts workflow_protocol kwarg with default True."""
    import inspect
    from coda_mcp.task_manager import create_task

    sig = inspect.signature(create_task)
    assert "workflow_protocol" in sig.parameters
    assert sig.parameters["workflow_protocol"].default is True


def test_create_task_forwards_workflow_protocol_to_wrap_prompt(monkeypatch, tmp_path):
    """create_task must pass workflow_protocol through to wrap_prompt."""
    from coda_mcp import task_manager

    captured: dict = {}

    def fake_wrap_prompt(**kwargs):
        captured.update(kwargs)
        return "DUMMY PROMPT"

    monkeypatch.setattr(task_manager, "wrap_prompt", fake_wrap_prompt)
    monkeypatch.setattr(task_manager, "_session_dir", lambda sid: str(tmp_path))
    monkeypatch.setattr(task_manager, "_task_dir", lambda sid, tid: str(tmp_path))
    monkeypatch.setattr(task_manager, "_write_task_meta", lambda *a, **kw: None)
    monkeypatch.setattr(task_manager.os, "makedirs", lambda *a, **kw: None)
    # Stub the file-open for prompt.txt write.
    real_open = open
    def fake_open(path, mode="r", *args, **kwargs):
        if "prompt.txt" in str(path) and "w" in mode:
            import io
            return io.StringIO()
        return real_open(path, mode, *args, **kwargs)
    monkeypatch.setattr("builtins.open", fake_open)

    task_manager.create_task(
        session_id="s-1",
        prompt="x",
        email="u@example.com",
        workflow_protocol=False,
    )
    assert captured.get("workflow_protocol") is False


def test_coda_run_signature_has_workflow_protocol_param():
    """coda_run accepts workflow_protocol kwarg with default True."""
    import inspect
    from coda_mcp import mcp_server

    sig = inspect.signature(mcp_server.coda_run)
    assert "workflow_protocol" in sig.parameters
    assert sig.parameters["workflow_protocol"].default is True
```

- [ ] **Step 3: Run the new tests; verify they FAIL**

Run: `uv run pytest tests/test_task_manager.py -v` (or whichever file you appended to)
Expected: All 6 new tests FAIL — `wrap_prompt`/`create_task`/`coda_run` don't accept the kwarg yet.

- [ ] **Step 4: Modify `coda_mcp/task_manager.py:153` — `wrap_prompt` signature + body**

Open `coda_mcp/task_manager.py` and find the existing `wrap_prompt` function (starts around line 153). Change its signature and body as follows.

Add a new import at the top of the file (if not already present, near other coda_mcp imports):

```python
from coda_mcp.databricks_preamble import build_capabilities, build_workflow_protocol
```

Then change the function signature from:

```python
def wrap_prompt(
    task_id: str,
    session_id: str,
    email: str,
    prompt: str,
    context: dict | None,
    results_dir: str,
    context_hint: str | None = None,
    previous_session_id: str | None = None,
) -> str:
```

to:

```python
def wrap_prompt(
    task_id: str,
    session_id: str,
    email: str,
    prompt: str,
    context: dict | None,
    results_dir: str,
    context_hint: str | None = None,
    previous_session_id: str | None = None,
    workflow_protocol: bool = True,
) -> str:
```

Update the docstring to mention the new flag:

```python
"""Build the full prompt string written to ``prompt.txt``.

Uses the ``---CODA-TASK---`` envelope convention so the agent can
parse metadata from the prompt deterministically.

When ``workflow_protocol`` is True (default), inserts a CAPABILITIES
section (Databricks CLI, skills, MCP servers) and a WORKFLOW PROTOCOL
section (3-phase PLAN/EXECUTE/SYNTHESIZE with critique at each phase,
plus the info_needed escape hatch). Set False to skip both.
"""
```

Update the body. The current return statement looks roughly like this (around lines 184-225):

```python
return (
    f"---CODA-TASK---\n"
    ...
    f"TASK:\n"
    f"{prompt}\n"
    f"\n"
    f"INSTRUCTIONS:\n"
    ...
    f"SAFETY:\n"
    ...
    f"---END-CODA-TASK---"
)
```

Change it to insert the new sections between TASK and INSTRUCTIONS:

```python
workflow_block = ""
if workflow_protocol:
    workflow_block = (
        f"\nCAPABILITIES:\n"
        f"{build_capabilities()}\n"
        f"\n"
        f"WORKFLOW PROTOCOL:\n"
        f"{build_workflow_protocol()}\n"
    )

return (
    f"---CODA-TASK---\n"
    f"task_id: {task_id}\n"
    f"session_id: {session_id}\n"
    f"user: {email}\n"
    f"{hint_line}"
    f"{prior_session_block}"
    f"{context_block}\n"
    f"TASK:\n"
    f"{prompt}\n"
    f"{workflow_block}"
    f"\n"
    f"INSTRUCTIONS:\n"
    f"1. As you work, append progress lines to {results_dir}/status.jsonl\n"
    f'   Each line must be valid JSON: {{"step": "label", "message": "what you are doing"}}\n'
    f"\n"
    f"2. When you are COMPLETELY DONE, write a SINGLE FILE at this exact path:\n"
    f"   {results_dir}/result.json\n"
    f"   It must contain this JSON structure:\n"
    f"   {{\n"
    f'     "status": "completed",\n'
    f'     "summary": "one paragraph describing what you did",\n'
    f'     "files_changed": ["list", "of", "file", "paths"],\n'
    f'     "artifacts": {{}},\n'
    f'     "errors": []\n'
    f"   }}\n"
    f"   If you failed, set status to \"failed\" and describe the error.\n"
    f"   IMPORTANT: result.json is a FILE not a directory. Write it with:\n"
    f"   echo '{{...}}' > {results_dir}/result.json\n"
    f"\n"
    f"3. If you delegate to a sub-agent, update status.jsonl with delegation steps.\n"
    f"\n"
    f"SAFETY:\n"
    f"- Do NOT delete, drop, or truncate tables, schemas, catalogs, or volumes.\n"
    f"- Do NOT delete files outside the current project directory.\n"
    f"- Do NOT run destructive Databricks CLI commands (e.g. databricks clusters delete, "
    f"databricks jobs delete, databricks pipelines delete).\n"
    f"- Do NOT modify permissions, grants, or access controls unless explicitly requested.\n"
    f"- Prefer CREATE OR REPLACE over DROP+CREATE. Prefer INSERT/MERGE over DELETE+INSERT.\n"
    f"- If the task requires a destructive operation, describe what you would do in "
    f"result.json with status \"needs_approval\" instead of executing it.\n"
    f"---END-CODA-TASK---"
)
```

Note: the INSTRUCTIONS body itself is updated in Task 3 to mention `info_needed` and the new step labels. For this task, leave the INSTRUCTIONS text exactly as today — only insert the new sections.

- [ ] **Step 5: Modify `coda_mcp/task_manager.py:231` — `create_task` signature + forward**

Find the `create_task` function (starts around line 231). Add `workflow_protocol: bool = True` to its parameter list (alongside the existing kwargs like `timeout_s`, `permissions`, `previous_session_id`). Forward it into the `wrap_prompt` call inside the function body.

The existing function probably looks like:

```python
def create_task(
    session_id: str,
    prompt: str,
    email: str,
    context: dict | None = None,
    context_hint: str | None = None,
    timeout_s: int | None = None,
    permissions: str | None = None,
    previous_session_id: str | None = None,
):
    ...
    wrapped = wrap_prompt(
        task_id=task_id,
        session_id=session_id,
        email=email,
        prompt=prompt,
        context=context,
        results_dir=results_dir,
        context_hint=context_hint,
        previous_session_id=previous_session_id,
    )
    ...
```

Change to:

```python
def create_task(
    session_id: str,
    prompt: str,
    email: str,
    context: dict | None = None,
    context_hint: str | None = None,
    timeout_s: int | None = None,
    permissions: str | None = None,
    previous_session_id: str | None = None,
    workflow_protocol: bool = True,
):
    ...
    wrapped = wrap_prompt(
        task_id=task_id,
        session_id=session_id,
        email=email,
        prompt=prompt,
        context=context,
        results_dir=results_dir,
        context_hint=context_hint,
        previous_session_id=previous_session_id,
        workflow_protocol=workflow_protocol,
    )
    ...
```

- [ ] **Step 6: Modify `coda_mcp/mcp_server.py:220` — `coda_run` signature + forward**

Find the `coda_run` function (starts around line 220). Add `workflow_protocol: bool = True` to its parameter list and pass it to `task_manager.create_task`.

Current signature:

```python
async def coda_run(
    prompt: str,
    email: str,
    context: str = "{}",
    previous_session_id: str = "",
    permissions: str = "smart",
    timeout_s: int = 3600,
) -> str:
```

Change to:

```python
async def coda_run(
    prompt: str,
    email: str,
    context: str = "{}",
    previous_session_id: str = "",
    permissions: str = "smart",
    timeout_s: int = 3600,
    workflow_protocol: bool = True,
) -> str:
```

Update the docstring (the existing string ends "Returns JSON with ``task_id``, ``session_id``, and ``status: \"running\"``"). Add this sentence to the docstring body before the Returns line:

```
``workflow_protocol`` defaults to True, which injects a Databricks
orientation block and a 3-phase workflow protocol (PLAN/EXECUTE/SYNTHESIZE
with critique at each phase) into the agent's prompt. The protocol also
defines the ``info_needed`` terminal status for clean handoff when the
agent is blocked. Set False to skip — useful for non-Databricks tasks.
```

Find the `task_manager.create_task(...)` call (around line 265) and add the new kwarg:

```python
result = task_manager.create_task(
    session_id=session_id,
    prompt=prompt,
    email=email,
    context=ctx,
    timeout_s=timeout_s,
    permissions=permissions,
    previous_session_id=previous_session_id or None,
    workflow_protocol=workflow_protocol,
)
```

- [ ] **Step 7: Run the new tests; verify they PASS**

Run: `uv run pytest tests/test_task_manager.py -v` (or whichever file)
Expected: All 6 new tests PASS.

Also run the full target file plus the new module's tests to check no regression:

```
uv run pytest tests/test_databricks_preamble.py tests/test_task_manager.py tests/test_coda_interactive.py -v
```

Expected: All pass. If any fail, fix the implementation (not the tests).

- [ ] **Step 8: Run ruff**

Run: `uv run ruff check coda_mcp/task_manager.py coda_mcp/mcp_server.py tests/test_task_manager.py`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add coda_mcp/task_manager.py coda_mcp/mcp_server.py tests/test_task_manager.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat: wire workflow_protocol flag through coda_run → create_task → wrap_prompt

The flag defaults to True. When set, wrap_prompt inserts CAPABILITIES and
WORKFLOW PROTOCOL sections between TASK and INSTRUCTIONS in prompt.txt.
Callers can opt out via workflow_protocol=False on coda_run for purely
non-Databricks tasks."
```

---

## Task 3: Update INSTRUCTIONS section to document `info_needed` + new step labels

The INSTRUCTIONS block in `wrap_prompt` still says only "If you failed, set status to 'failed'" — silent about `info_needed`. Update it.

**Files:**
- Modify: `coda_mcp/task_manager.py:153-225` (INSTRUCTIONS portion of `wrap_prompt`'s return)
- Modify (or extend): `tests/test_task_manager.py`

- [ ] **Step 1: Append the pinning tests to `tests/test_task_manager.py`**

```python
def test_wrap_prompt_instructions_documents_info_needed():
    """INSTRUCTIONS section must mention the info_needed status and feedback field."""
    from coda_mcp.task_manager import wrap_prompt

    out = wrap_prompt(
        task_id="t-1",
        session_id="s-1",
        email="user@example.com",
        prompt="do the thing",
        context=None,
        results_dir="/tmp/r",
    )
    # Pull the INSTRUCTIONS section out for focused assertions.
    assert "info_needed" in out
    assert "feedback" in out


def test_wrap_prompt_instructions_lists_new_step_labels():
    """INSTRUCTIONS section enumerates the canonical step labels emitted by the agent."""
    from coda_mcp.task_manager import wrap_prompt

    out = wrap_prompt(
        task_id="t-1",
        session_id="s-1",
        email="user@example.com",
        prompt="do the thing",
        context=None,
        results_dir="/tmp/r",
    )
    for label in ("plan", "critique_plan", "execute", "critique_execute", "synthesize", "critique_synthesize"):
        assert label in out, f"Missing step label {label!r} from prompt text"
```

- [ ] **Step 2: Run; verify FAIL**

Run: `uv run pytest tests/test_task_manager.py::test_wrap_prompt_instructions_documents_info_needed tests/test_task_manager.py::test_wrap_prompt_instructions_lists_new_step_labels -v`
Expected: both FAIL.

- [ ] **Step 3: Update the INSTRUCTIONS section in `wrap_prompt`**

In `coda_mcp/task_manager.py`, find the line that says `f'   Each line must be valid JSON: ...'` (currently around line 197). Replace the entire INSTRUCTIONS portion (steps 1, 2, 3) with this:

```python
f"INSTRUCTIONS:\n"
f"1. As you work, append progress lines to {results_dir}/status.jsonl\n"
f'   Each line must be valid JSON: {{"step": "label", "message": "what you are doing"}}\n'
f"   Canonical step labels (use these when the workflow protocol is active):\n"
f"     plan, critique_plan, execute_<n>, critique_execute,\n"
f"     synthesize, critique_synthesize, info_needed, failed\n"
f"\n"
f"2. When you are COMPLETELY DONE, write a SINGLE FILE at this exact path:\n"
f"   {results_dir}/result.json\n"
f"   It must contain this JSON structure (status field has four allowed values):\n"
f"   {{\n"
f'     "status": "completed" | "failed" | "info_needed" | "needs_approval",\n'
f'     "summary": "one paragraph describing what you did or why you stopped",\n'
f'     "feedback": "REQUIRED if status=info_needed — what context the caller must add",\n'
f'     "files_changed": ["list", "of", "file", "paths"],\n'
f'     "artifacts": {{}},\n'
f'     "errors": []\n'
f"   }}\n"
f"   - status=\"completed\": you finished the task.\n"
f"   - status=\"failed\": unrecoverable hard error; describe in errors[].\n"
f"   - status=\"info_needed\": you are blocked because something the CALLER must\n"
f"     supply is missing. The feedback field is REQUIRED and must precisely\n"
f"     name what is missing. The caller will resubmit with more context.\n"
f"   - status=\"needs_approval\": you have a destructive action ready but need\n"
f"     explicit caller approval before executing. See SAFETY section.\n"
f"   IMPORTANT: result.json is a FILE not a directory. Write it with:\n"
f"   echo '{{...}}' > {results_dir}/result.json\n"
f"\n"
f"3. If you delegate to a sub-agent, update status.jsonl with delegation steps.\n"
f"\n"
```

The block above replaces the OLD INSTRUCTIONS steps 1-3 ENTIRELY. The SAFETY section below it stays unchanged.

- [ ] **Step 4: Run; verify GREEN**

Run: `uv run pytest tests/test_task_manager.py -v`
Expected: all task_manager tests pass.

Run: `uv run pytest tests/test_databricks_preamble.py tests/test_task_manager.py tests/test_coda_interactive.py -v`
Expected: still green across the board.

- [ ] **Step 5: Ruff check**

Run: `uv run ruff check coda_mcp/task_manager.py tests/test_task_manager.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add coda_mcp/task_manager.py tests/test_task_manager.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat: document info_needed status and canonical step labels in INSTRUCTIONS

The INSTRUCTIONS section of prompt.txt now enumerates the four allowed
result.json status values (completed, failed, info_needed, needs_approval),
describes when to use each, and lists the canonical status.jsonl step
labels emitted by the workflow protocol."
```

---

## Task 4: Update surfaces — counts dict, get_result docstring, MCP instructions paragraph (TDD)

Three small surface updates that together let upstream callers understand the new statuses.

**Files:**
- Modify: `coda_mcp/mcp_server.py:551-559` (counts dict in `coda_inbox`)
- Modify: `coda_mcp/mcp_server.py:573-584` (`coda_get_result` docstring)
- Modify: `coda_mcp/mcp_server.py:52-99` (FastMCP `instructions=` block)
- Create: `tests/test_inbox_status_passthrough.py`

- [ ] **Step 1: Create the test file `tests/test_inbox_status_passthrough.py`**

```python
"""Tests covering counts dict, coda_get_result docstring, and MCP instructions
all reflect the new info_needed / needs_approval terminal statuses."""
import asyncio
import json


def test_mcp_instructions_mention_info_needed():
    """Server-level MCP instructions teach calling LLMs about info_needed."""
    from coda_mcp import mcp_server

    txt = mcp_server.mcp.instructions
    assert "info_needed" in txt
    assert "needs_approval" in txt
    assert "feedback" in txt


def test_coda_get_result_docstring_mentions_info_needed():
    """coda_get_result docstring lists info_needed / needs_approval alongside completed/failed."""
    from coda_mcp import mcp_server

    doc = (mcp_server.coda_get_result.__doc__ or "").lower()
    assert "info_needed" in doc
    assert "needs_approval" in doc


def test_inbox_counts_dict_includes_new_statuses(monkeypatch):
    """coda_inbox counts dict has info_needed and needs_approval keys."""
    from coda_mcp import mcp_server

    fake_tasks = [
        {"task_id": "t1", "session_id": "s1", "status": "running"},
        {"task_id": "t2", "session_id": "s2", "status": "completed"},
        {"task_id": "t3", "session_id": "s3", "status": "failed"},
        {"task_id": "t4", "session_id": "s4", "status": "info_needed"},
        {"task_id": "t5", "session_id": "s5", "status": "needs_approval"},
        {"task_id": "t6", "session_id": "s6", "status": "info_needed"},
    ]

    monkeypatch.setattr(
        mcp_server.task_manager, "list_all_tasks",
        lambda email, status_filter=None: list(fake_tasks),
    )
    # _read_session_safe is called inside the loop; return None so no viewer_url is added.
    monkeypatch.setattr(
        mcp_server.task_manager, "_read_session_safe", lambda sid: None,
    )

    result_str = asyncio.run(mcp_server.coda_inbox(email="u@e"))
    result = json.loads(result_str)
    counts = result["counts"]

    assert counts["running"] == 1
    assert counts["completed"] == 1
    assert counts["failed"] == 1
    assert counts["info_needed"] == 2
    assert counts["needs_approval"] == 1
```

- [ ] **Step 2: Run; verify FAIL**

Run: `uv run pytest tests/test_inbox_status_passthrough.py -v`
Expected: all 3 tests FAIL — instructions don't mention info_needed, docstring doesn't, and counts dict has only 3 keys.

- [ ] **Step 3: Update the FastMCP `instructions=` block in `coda_mcp/mcp_server.py:52-99`**

Find the `mcp = FastMCP(...)` constructor (starts around line 50). Inside the `instructions=` argument is a multi-line string concatenation. Locate the existing "CHAINING" paragraph (the one that says `"CHAINING: pass previous_session_id ..."`). After that paragraph and BEFORE the "SHARE THE REPLAY URL" paragraph, insert this new paragraph:

```python
        "INFO_NEEDED HANDOFF: When coda_inbox shows a task with status='info_needed', "
        "the agent could not proceed because of missing context. Call coda_get_result "
        "to read the 'feedback' field — it tells you exactly what the agent needs (a "
        "table name, a decision, a clarification). Add that context to the prompt and "
        "resubmit via coda_run with previous_session_id set to the original task's "
        "session_id so the agent has the prior attempt's context. 'needs_approval' is "
        "similar but means the agent has a destructive plan and is waiting for the "
        "caller's explicit go/no-go.\n\n"
```

Make sure the trailing newlines match the surrounding string concatenation (the other paragraphs end with `\n\n`).

- [ ] **Step 4: Update the counts dict in `coda_inbox` (lines 551-559)**

Find this block:

```python
counts = {"running": 0, "completed": 0, "failed": 0}
for t in tasks:
    s = t.get("status", "")
    if s in counts:
        counts[s] += 1
    elif s == "done":
        counts["completed"] += 1
    elif s == "timeout":
        counts["failed"] += 1
```

Change the first line to add the two new keys:

```python
counts = {
    "running": 0,
    "completed": 0,
    "failed": 0,
    "info_needed": 0,
    "needs_approval": 0,
}
for t in tasks:
    s = t.get("status", "")
    if s in counts:
        counts[s] += 1
    elif s == "done":
        counts["completed"] += 1
    elif s == "timeout":
        counts["failed"] += 1
```

The aliasing branches (`done`, `timeout`) are unchanged.

- [ ] **Step 5: Update `coda_get_result` docstring (line ~579)**

Find the docstring of `coda_get_result`:

```python
"""Retrieve the structured result of a completed task.

Call this AFTER coda_inbox shows a task as "completed" or "failed".

Returns JSON with ``task_id``, ``session_id``, ``status``, ``summary``
(what was done), ``files_changed`` (list of modified files),
``artifacts`` (job IDs, commit hashes, etc.), and ``errors`` (if any).
"""
```

Change to:

```python
"""Retrieve the structured result of a completed task.

Call this AFTER coda_inbox shows a task as "completed", "failed",
"info_needed", or "needs_approval".

Returns JSON with ``task_id``, ``session_id``, ``status``, ``summary``
(what was done or why the agent stopped), ``files_changed`` (list of
modified files), ``artifacts`` (job IDs, commit hashes, etc.),
``errors`` (if any), and — when status is "info_needed" — ``feedback``
(a precise description of what context the caller must add before
resubmitting).
"""
```

- [ ] **Step 6: Run the new tests; verify GREEN**

Run: `uv run pytest tests/test_inbox_status_passthrough.py -v`
Expected: 3 passed.

- [ ] **Step 7: Run target-area tests to verify no regression**

Run: `uv run pytest tests/test_inbox_status_passthrough.py tests/test_coda_interactive.py tests/test_databricks_preamble.py tests/test_task_manager.py -v`
Expected: all pass.

- [ ] **Step 8: Ruff**

Run: `uv run ruff check coda_mcp/mcp_server.py tests/test_inbox_status_passthrough.py`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  add coda_mcp/mcp_server.py tests/test_inbox_status_passthrough.py
git -c user.email=datasciencemonkey@gmail.com -c user.name="Sathish Gangichetty" \
  commit -m "feat: surface info_needed + needs_approval in inbox counts, get_result doc, MCP instructions

Three surfaces updated so calling LLMs and dashboards know about the
two soft terminal statuses:
- coda_inbox counts dict gains info_needed and needs_approval keys.
- coda_get_result docstring lists all four valid statuses and the
  feedback field that accompanies info_needed.
- FastMCP server-level instructions gain an INFO_NEEDED HANDOFF
  paragraph teaching upstream LLMs to read 'feedback' and resubmit
  with previous_session_id for the chained context."
```

---

## Task 5: Push branch and update PR #67 description

**Files:**
- None (remote/PR update only)

- [ ] **Step 1: Verify branch state**

```bash
git status
git log --oneline origin/feat/coda-mcp-interactive-handoff..HEAD
```

Expected: working tree clean. The new commits since the last push include the spec, the spec-critic fixes, the plan, and the four implementation commits (Tasks 1-4).

- [ ] **Step 2: Push**

```bash
git push origin feat/coda-mcp-interactive-handoff
```

Expected: fast-forward push.

- [ ] **Step 3: Append a follow-up section to PR #67 body**

Read the current body:

```bash
gh pr view 67 --json body -q .body > /tmp/pr67-body.md
```

Append this section:

```
---

## Follow-up #2: Workflow protocol + Databricks orientation

`coda_run` now injects two new sections into `prompt.txt`:
- **CAPABILITIES** — tells hermes about the Databricks CLI (pre-authed), the 16 Databricks skills under `~/.claude/skills/`, and the DeepWiki / Exa / CoDA MCP servers.
- **WORKFLOW PROTOCOL** — imposes a 3-phase pipeline (PLAN → EXECUTE → SYNTHESIZE) with a critique step after each phase (self-review or sub-agent — agent's choice). Max 2 iterations per phase to keep token cost bounded.

New terminal `result.json` status `"info_needed"` with a required `feedback` field gives the calling client a structured iteration loop when the agent is blocked. The existing `"needs_approval"` status is preserved with explicit disambiguation: `info_needed` = "caller must add context"; `needs_approval` = "caller must approve a destructive action".

**Three surfaces updated** so upstream LLMs know about the new statuses:
- `coda_inbox` counts dict gains `info_needed` and `needs_approval` keys.
- `coda_get_result` docstring lists all four valid statuses + the new `feedback` field.
- FastMCP server-level instructions gain an INFO_NEEDED HANDOFF paragraph.

**Flag:** `coda_run(... workflow_protocol=True)` is the default. Set False to skip both new sections for non-Databricks tasks.

**Artifacts:**
- Spec: `docs/superpowers/specs/2026-05-28-coda-run-workflow-protocol-design.md`
- Plan: `docs/superpowers/plans/2026-05-28-coda-run-workflow-protocol.md`
```

Then update the PR body:

```bash
gh pr edit 67 --body-file /tmp/pr67-body.md
```

Or if gh's TLS bug hits on this machine, fall back to curl + REST per the prior follow-up.

- [ ] **Step 4: Confirm**

Run `gh pr view 67 --json body -q .body | tail -30` and verify the new section appears.

---

## Self-review of this plan against the spec

**Spec section 1 — Goal.** Task 1 creates the module; Task 2 wires the flag; Task 3 updates INSTRUCTIONS; Task 4 surfaces the statuses. ✓

**Spec section "Components" 1 (databricks_preamble.py).** Task 1 creates it with all three exports. ✓

**Components 2 + 3 (CAPABILITIES + WORKFLOW PROTOCOL content).** Task 1's module has the verbatim text from the spec. ✓

**Components 4 (expanded INSTRUCTIONS).** Task 3 covers it. ✓

**Components 5 (task_manager changes).** Task 2 covers wrap_prompt and create_task. ✓

**Components 6 (mcp_server.coda_run changes).** Task 2 covers it. ✓

**Components 7 (counts dict + get_result docstring).** Task 4 covers both. ✓

**Components 7a (MCP instructions string).** Task 4 covers it. ✓

**Components 7b (watcher interaction).** Documented in spec as no-code-change. Plan does not need a task for it.

**Testing strategy.** Every test listed in the spec maps to a task step in Task 1 (`test_databricks_preamble.py`), Task 2 (extension of `test_task_manager.py`), Task 3 (further extension of same), Task 4 (`test_inbox_status_passthrough.py`). ✓

**Acceptance criteria 1-8.** All mapped. ✓

**Placeholder scan:** No TBD/TODO. Every step has explicit code or commands.

**Type consistency:** `workflow_protocol: bool = True` used uniformly across all three call sites (wrap_prompt, create_task, coda_run). Step labels (`plan`, `critique_plan`, etc.) match between Task 1's module text, Task 3's INSTRUCTIONS update, and the spec.

**Risk: Task 2 Step 5 might leave the `_write_task_meta` mock or other internal helpers' signatures stale.** The test `test_create_task_forwards_workflow_protocol_to_wrap_prompt` monkeypatches `_session_dir`, `_task_dir`, `_write_task_meta`, and `os.makedirs`. If `create_task` calls additional helpers in production, the test will fail with cryptic AttributeError. If that happens during execution, add the missing helpers to the monkeypatch list — the test's intent is to verify ONLY the flag pass-through, not the file-system side effects.

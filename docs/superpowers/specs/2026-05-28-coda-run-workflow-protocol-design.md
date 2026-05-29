# Spec: `coda_run` Workflow Protocol + Databricks Orientation

**Status:** Draft, pre-critique-gate
**Date:** 2026-05-28
**Branch:** `feat/coda-mcp-interactive-handoff` (continues PR #67) or follow-up branch
**Related:**
- `docs/superpowers/specs/2026-05-28-coda-interactive-mcp-tool-design.md` (Mode 2)
- `docs/superpowers/specs/2026-05-28-coda-run-replay-only-design.md` (Mode 3 narrowing)

## Goal

When a caller invokes `coda_run`, the background hermes session should:

1. **Know** it is running inside a Databricks-authenticated environment with skills, CLI, and MCP servers available.
2. **Follow** a structured 3-phase workflow (PLAN → EXECUTE → SYNTHESIZE) with a critique step after each phase.
3. **Escape cleanly** when blocked — emit `status="info_needed"` with structured feedback so the calling client can iterate.

Both behaviors are imposed by writing a richer prompt envelope into the `prompt.txt` file that hermes reads. No PTY-timing hacks, no agent-specific config.

## Why

Today's `wrap_prompt` (`task_manager.py:153`) gives the agent: TASK, INSTRUCTIONS (status/result file contract), and SAFETY (don't-delete guardrails). It does NOT tell the agent:
- What capabilities exist on the host (Databricks CLI, skills, MCP servers).
- HOW to work the task (just-jump-in vs plan-first vs self-review).
- WHAT to do when blocked (today, the agent either invents an answer or fails hard).

The fix is to extend the prompt envelope with two new sections — CAPABILITIES and WORKFLOW PROTOCOL — and a new terminal status, `info_needed`.

## Non-goals

- Not changing hermes itself. The protocol is enforced via prompt content; if hermes ignores it, that's a hermes problem to chase separately.
- Not adding protocol enforcement to `coda_interactive`. Interactive sessions are human-driven.
- Not adding dynamic skill discovery. The Databricks skill list is hardcoded; staleness is caught by tests, not runtime introspection.
- Not changing the result.json file location, file name, or top-level convention. Only the value of `status` and the addition of an optional `feedback` field.

---

## Architecture

```
coda_run(prompt, ..., workflow_protocol=True)
   │
   ▼
task_manager.create_task(..., workflow_protocol=True)
   │
   ▼
task_manager.wrap_prompt(..., workflow_protocol=True)
   │
   ▼
prompt.txt now contains:
   ---CODA-TASK---
   metadata...
   TASK: <user prompt>

   CAPABILITIES:                 ← from coda_mcp/databricks_preamble.py::build_capabilities()
   <orientation block>

   WORKFLOW PROTOCOL:            ← from coda_mcp/databricks_preamble.py::build_workflow_protocol()
   <3-phase + info_needed instructions>

   INSTRUCTIONS:                 ← existing status.jsonl + result.json contract,
   <expanded with new step labels and info_needed status>

   SAFETY:                       ← unchanged
   <guardrails>
   ---END-CODA-TASK---
   │
   ▼
hermes -z "/path/to/prompt.txt"
   │
   ▼
Hermes works the task, emits status.jsonl, writes result.json
   │
   ▼
coda_inbox / coda_get_result surface the result, including new "info_needed" status
```

---

## Components

### 1. New module: `coda_mcp/databricks_preamble.py`

Exposes pure-function builders that produce the two new prompt sections. Pure functions for testability — no I/O, no global state.

```python
"""Builders for the CoDA workflow prompt envelope sections.

These produce static text that is injected into prompt.txt by
``task_manager.wrap_prompt``. Pure functions — no side effects.
"""

_DATABRICKS_SKILLS = (
    "agent-bricks", "databricks-genie", "databricks-app-python",
    "databricks-app-apx", "databricks-jobs", "databricks-unity-catalog",
    "spark-declarative-pipelines", "aibi-dashboards", "model-serving",
    "mlflow-evaluation", "asset-bundles", "databricks-python-sdk",
    "databricks-config", "databricks-docs", "synthetic-data-generation",
    "unstructured-pdf-generation",
)

def build_capabilities() -> str:
    """Orientation block: CLI, skills, MCP servers, when to prefer them."""

def build_workflow_protocol() -> str:
    """3-phase workflow (PLAN/EXECUTE/SYNTHESIZE) + critique + info_needed."""

def get_databricks_skills() -> tuple[str, ...]:
    """Return the canonical skill list. Used by tests to pin the catalog."""
    return _DATABRICKS_SKILLS
```

### 2. `CAPABILITIES:` section content (verbatim)

```
You are running inside CoDA on a Databricks-authenticated host.

Databricks CLI: pre-configured. `databricks current-user me` confirms auth.
Use it for jobs, workspace, clusters, warehouses, Unity Catalog operations.

Skills available at ~/.claude/skills/ — read each skill's SKILL.md before
invoking. Relevant Databricks skills:
- agent-bricks, databricks-genie, databricks-app-python, databricks-app-apx
- databricks-jobs, databricks-unity-catalog, spark-declarative-pipelines
- aibi-dashboards, model-serving, mlflow-evaluation, asset-bundles
- databricks-python-sdk, databricks-config, databricks-docs
- synthetic-data-generation, unstructured-pdf-generation

MCP servers wired:
- DeepWiki — ask_question, read_wiki_contents for any GitHub repo
- Exa — web_search_exa, web_fetch_exa for live web context
- CoDA — chain follow-up tasks via previous_session_id

When the task touches Databricks data, pipelines, jobs, dashboards, agents,
or model serving, DEFAULT to the skill / CLI / SDK path above instead of
generic Python or web search.
```

### 3. `WORKFLOW PROTOCOL:` section content (verbatim)

```
You MUST process this task in three phases. Emit status.jsonl events as
you go (one JSON object per line, format below).

PHASE 1 — PLAN
- Write a step-by-step plan as a status.jsonl line with step="plan" and
  message containing the numbered steps.
- Then critique your own plan as if you were a separate reviewer.
  (Spawn a sub-agent for the critique if your agent supports it; otherwise
  write the critique inline as a self-review.) Emit step="critique_plan"
  with the verdict (APPROVE / BLOCK / APPROVE-WITH-FIXES) and findings.
- If the critique surfaces blockers, revise the plan once and re-emit
  step="plan". Maximum 2 plan iterations total.
- If after 2 attempts you still cannot produce a viable plan, write
  result.json with status="info_needed" (see below) and stop.

PHASE 2 — EXECUTE
- Work the plan. Emit step="execute_<n>" lines after completing each plan
  step (n is 1-indexed, matches the plan's numbering).
- After execution, emit step="critique_execute" with a review of what got
  built vs what the plan said. APPROVE / BLOCK / APPROVE-WITH-FIXES.
- If the critique surfaces correctness or scope gaps, fix them and re-emit
  step="critique_execute". Maximum 2 execute iterations total.
- If you hit a hard blocker (missing access, missing data, ambiguous
  requirements that the plan revealed only mid-execution), write
  result.json with status="info_needed" and stop.

PHASE 3 — SYNTHESIZE
- Write result.json with status="completed".
- Emit step="critique_synthesize" with a review of the result against the
  original TASK.
- If the critique surfaces gaps, revise result.json. Maximum 2 synthesis
  iterations total.

If at any phase you cannot proceed, use the INFO_NEEDED escape hatch:
- Set status="info_needed" in result.json.
- Set "feedback" to a precise, actionable string naming exactly what is
  missing (a table name, a decision, an access grant, a clarification).
  The calling client will read this and resubmit with the missing context.
- "info_needed" is NOT a failure — it is a structured request for
  iteration. Use it whenever you would otherwise have to guess.

If you encounter a hard, unrecoverable failure (a command crashed, an SDK
returned 500, a file is corrupt), use status="failed" with a description
in "errors".

DISAMBIGUATION — two soft statuses already exist and they mean different
things; use the right one:
- "info_needed" — the CALLER must add missing context (table name,
  business decision, file contents, access grant) before the task can
  proceed. Used when ambiguity or missing input blocks you.
- "needs_approval" — you have a concrete plan to do something destructive
  (drop a table, delete a job, modify permissions). You will execute it
  if and only if the caller explicitly approves. Used at the SAFETY
  boundary, never for ambiguity. See SAFETY section below.

If both apply (e.g. "I'd drop a table but I'm not sure which one"), prefer
"info_needed" — resolving the ambiguity first is cheaper than approving
the wrong destructive action.
```

### 4. Expanded `INSTRUCTIONS:` content

The existing INSTRUCTIONS block grows to enumerate the new step labels and the new status. The actual labels and the result.json schema additions appear here for the agent's reference.

New result.json `status` values: `"completed"` | `"failed"` | `"info_needed"`.

When `status="info_needed"`, the `feedback` field is REQUIRED and must be a string ≥ 20 chars.

```json
{
  "status": "info_needed",
  "summary": "Could not proceed: <one-line reason>",
  "feedback": "Specific question or missing context the calling client must supply before resubmit. Name the table, field, decision, or access that's missing.",
  "files_changed": ["..."],
  "artifacts": {},
  "errors": []
}
```

### 5. `coda_mcp/task_manager.py` changes

- `wrap_prompt()` gains a parameter: `workflow_protocol: bool = True`.
- When `True`, inserts the CAPABILITIES and WORKFLOW PROTOCOL sections between TASK and INSTRUCTIONS. When `False`, the prompt looks like today.
- `create_task()` gains the same parameter and forwards it.
- Update the existing INSTRUCTIONS section text to enumerate the new step labels (`plan`, `critique_plan`, `execute_<n>`, `critique_execute`, `synthesize`, `critique_synthesize`, `info_needed`) and the new result.json status options.

### 6. `coda_mcp/mcp_server.py` changes

`coda_run` gains `workflow_protocol: bool = True` parameter, passed straight through to `create_task`. The tool's docstring is updated to mention the parameter and its effect.

### 7. Inbox / result surfacing changes (REQUIRED — was previously deferred)

The current `coda_inbox` implementation at `coda_mcp/mcp_server.py:551` has a HARDCODED counts dict:

```python
counts = {"running": 0, "completed": 0, "failed": 0}
```

Tasks with `status="info_needed"` or `status="needs_approval"` would appear in the `tasks` list but the counts summary would show 0/0/0 — visibly broken. This must be fixed:

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

The `coda_get_result` docstring at `mcp_server.py:579` says:
> Call this AFTER coda_inbox shows a task as "completed" or "failed".

Must be updated to:
> Call this AFTER coda_inbox shows a task as "completed", "failed", "info_needed", or "needs_approval".

And the response should pass through the new `feedback` field (and the existing schema fields) verbatim — `task_manager.get_task_result` already returns the full result.json content, so no code change there beyond a regression test.

### 7a. MCP `instructions` string update (REQUIRED)

The server-level instructions block at `coda_mcp/mcp_server.py:52-99` is the document that teaches upstream LLM callers how to use these tools. Currently it says nothing about `info_needed`. Add a new paragraph (placed after the CHAINING paragraph at line 68):

```
INFO_NEEDED HANDOFF: When coda_inbox shows a task with status='info_needed',
the agent could not proceed because of missing context. Call
coda_get_result to read the 'feedback' field — it tells you exactly what
the agent needs (a table name, a decision, a clarification). Add that
context to the prompt and resubmit via coda_run with previous_session_id
set to the original task's session_id so the agent has the prior attempt's
context. 'needs_approval' is similar but means the agent has a destructive
plan and is waiting for the caller's explicit go/no-go.
```

### 7b. `_watch_task` interaction (sanity, no change required)

`_watch_task` in `mcp_server.py:134` polls for `result.json` and calls `task_manager.complete_task(session_id, task_id)` as soon as it appears. This is correct for all three terminal statuses: from a session-lifecycle perspective, a task that wrote a result.json IS done, regardless of whether the status is `completed`, `failed`, `info_needed`, or `needs_approval`. The session can be auto-closed; the status is preserved in result.json for the caller to read. No code change needed — but document this so the implementer doesn't second-guess.

---

## Data flow examples

### Happy path — task completes
1. Caller: `coda_run(prompt="build a UC dashboard", workflow_protocol=True)`.
2. `prompt.txt` contains CAPABILITIES + WORKFLOW PROTOCOL.
3. Hermes writes:
   - `step=plan`: 1. Use databricks-unity-catalog skill to list catalogs. 2. ...
   - `step=critique_plan`: APPROVE — plan is concrete and uses the right skill.
   - `step=execute_1`: listed 3 catalogs.
   - `step=execute_2`: built dashboard JSON via aibi-dashboards skill.
   - `step=critique_execute`: APPROVE — output matches plan.
   - `step=synthesize`: writing result.json.
   - `step=critique_synthesize`: APPROVE.
4. `result.json` has `status="completed"`.

### Blocked path — info_needed
1. Caller: `coda_run(prompt="add a column to the orders table", workflow_protocol=True)`.
2. `prompt.txt` contains CAPABILITIES + WORKFLOW PROTOCOL.
3. Hermes writes:
   - `step=plan`: 1. Identify orders table. 2. Determine column to add. 3. ...
   - `step=critique_plan`: BLOCK — "which orders table? Which schema/catalog? What column type?"
   - `step=info_needed`: terminal.
4. `result.json`:
   ```json
   {
     "status": "info_needed",
     "summary": "Could not proceed: ambiguous table reference",
     "feedback": "The prompt says 'orders table' but the workspace has 4 catalogs with 'orders' tables (main.sales.orders, dev.test.orders, staging.app.orders, prod.dwh.orders). Please specify the fully-qualified table name, and the column name + type to add.",
     ...
   }
   ```
5. Caller's MCP client sees `info_needed` in `coda_inbox`, reads the feedback, resubmits `coda_run` with the resolved table name and the original task's session ID via `previous_session_id`.

### Failed path — hard error
1. Caller: `coda_run(prompt="run my flaky pipeline", workflow_protocol=True)`.
2. Hermes plans, executes, then `databricks pipelines start ...` returns 500.
3. After retry, still 500. Agent decides this is unrecoverable from inside the task.
4. `result.json` has `status="failed"`, `errors=["pipeline API 500: ..."]`.
5. `info_needed` is NOT used — the caller cannot help by adding context; the problem is server-side.

---

## Testing strategy

### `tests/test_databricks_preamble.py` (new)

| Test | What it pins |
|------|--------------|
| `test_capabilities_mentions_cli` | Contains "Databricks CLI" |
| `test_capabilities_lists_at_least_10_skills` | At least 10 of `_DATABRICKS_SKILLS` appear in the rendered text |
| `test_capabilities_mentions_all_three_mcp_servers` | "DeepWiki", "Exa", "CoDA" each present |
| `test_capabilities_under_token_budget` | Length < 1600 chars (proxy for ~400 tokens) |
| `test_workflow_protocol_lists_three_phases` | Contains "PHASE 1 — PLAN", "PHASE 2 — EXECUTE", "PHASE 3 — SYNTHESIZE" |
| `test_workflow_protocol_caps_iterations_at_two` | Contains "Maximum 2" or "max 2" exactly 3 times (once per phase) |
| `test_workflow_protocol_describes_info_needed` | Contains "info_needed" and "feedback" |
| `test_skills_list_matches_claude_md` | Parse the "Databricks Skills" table from project CLAUDE.md; the set of skill names in that table must equal `set(get_databricks_skills())`. Catches drift in either direction (skill added to CLAUDE.md but not to the tuple, or vice versa). |

### `tests/test_task_manager.py` (extend)

| Test | What it pins |
|------|--------------|
| `test_wrap_prompt_with_workflow_protocol_default` | Output contains "CAPABILITIES:" and "WORKFLOW PROTOCOL:" |
| `test_wrap_prompt_workflow_protocol_false_omits_sections` | Both sections absent |
| `test_wrap_prompt_workflow_protocol_default_is_true` | Default param value is True |
| `test_wrap_prompt_lists_info_needed_in_instructions` | INSTRUCTIONS section mentions "info_needed" status |
| `test_wrap_prompt_lists_new_step_labels` | INSTRUCTIONS mentions plan, critique_plan, execute, etc. |
| `test_create_task_passes_workflow_protocol_through` | Mock-verify wrap_prompt receives the flag |

### `tests/test_mcp_server_coda_run.py` (extend or create)

| Test | What it pins |
|------|--------------|
| `test_coda_run_signature_has_workflow_protocol_param` | Inspect signature, default True |
| `test_coda_run_passes_workflow_protocol_to_create_task` | Monkeypatch create_task, assert kwarg received |

### `tests/test_inbox_status_passthrough.py` (new)

| Test | What it pins |
|------|--------------|
| `test_inbox_counts_dict_includes_info_needed_and_needs_approval` | Construct fake tasks with status="info_needed" and status="needs_approval"; call `coda_inbox`; assert counts dict contains both keys with correct values |
| `test_inbox_surfaces_info_needed_status` | Build a fake result.json with status="info_needed" and feedback="..." in a tmp results dir; call the inbox function; assert the new status comes through verbatim in the tasks list |
| `test_get_result_surfaces_feedback_field` | Same fixture; call `coda_get_result`; assert feedback field passes through |
| `test_mcp_instructions_mention_info_needed` | Read `mcp.instructions`; assert it contains "info_needed" and "needs_approval" |
| `test_get_result_docstring_mentions_info_needed` | Inspect `coda_get_result.__doc__`; assert it lists `info_needed` and `needs_approval` alongside `completed` / `failed` |

---

## Acceptance criteria

1. `coda_mcp/databricks_preamble.py` exists and exports `build_capabilities()`, `build_workflow_protocol()`, `get_databricks_skills()`.
2. `task_manager.wrap_prompt()` accepts `workflow_protocol: bool = True`; when True, inserts CAPABILITIES and WORKFLOW PROTOCOL sections; when False, omits them.
3. `task_manager.create_task()` forwards the flag.
4. `mcp_server.coda_run()` accepts `workflow_protocol: bool = True`; passes it through.
5. The 16 Databricks skills enumerated in `_DATABRICKS_SKILLS` match what CLAUDE.md documents.
6. New result.json status `"info_needed"` is described in the agent-facing INSTRUCTIONS and is allowed (not rejected) by inbox/result tooling.
7. All new tests in `tests/test_databricks_preamble.py`, plus extensions in `tests/test_task_manager.py` and `tests/test_inbox_status_passthrough.py`, pass.
8. Existing tests (especially the inbox/result tests) continue to pass.

---

## Risks

1. **Token cost.** Measured: CAPABILITIES ≈ 1050 chars (~260 tokens), WORKFLOW PROTOCOL ≈ 2280 chars (~570 tokens), plus an expanded INSTRUCTIONS section adds another ~100 tokens. Total: **~900 added tokens per task**. Acceptable because the agent gets oriented and disciplined; the flag lets callers opt out. (Earlier estimate of 600 was wrong — see spec history.)
2. **Hermes ignores the protocol.** If hermes treats the prompt as suggestion rather than contract, the structured phases may not appear in `status.jsonl`. Mitigation: not in scope for this spec — first ship the prompt content and measure adoption.
3. **Drift between hardcoded skill list and reality.** If skills are added/removed in CLAUDE.md, `_DATABRICKS_SKILLS` lies until updated. Mitigation: `test_skills_list_is_canonical` makes drift visible by failing.
4. **Critique loops eating tokens.** Max 2 iterations per phase is explicit in the protocol text. Mitigation built into the spec.
5. **`info_needed` status not surfaced in UI.** The viewer / dashboard rendering of `coda_inbox` may not have a visual treatment for `info_needed`. Out of scope for this spec — the protocol surfaces it in the JSON; rendering improvements are a separate change.

---

## Out of scope (explicit)

- Visual surfacing of `info_needed` in the inbox dashboard / viewer URL — defer.
- Dynamic skill discovery — defer.
- `coda_interactive` protocol enforcement — defer.
- Hermes-specific critic sub-agent mechanism — the protocol says "self-review OR sub-agent — agent's choice"; we don't dictate.
- Token-cost measurement / observability — defer.
- Status filtering in `coda_inbox` (e.g., "show only info_needed tasks") — defer.

---

## Migration notes

PR #67 is in flight on the same branch. This change can land as a follow-up commit on the same branch OR on a new branch. Recommend: same branch, new commits. The PR description gets a third follow-up section.

No existing callers depend on the absence of CAPABILITIES / WORKFLOW PROTOCOL sections. Adding them is additive.

The `workflow_protocol=False` escape hatch makes this safe to land even if the protocol turns out to be too aggressive — callers can opt out.

---

## Open question reserved for execution time

How does the existing `coda_inbox` / `coda_get_result` code handle unknown status strings today? If it normalizes them or filters them out, the implementation step needs to add `info_needed` to the allow list. If it's a pass-through, no change is needed beyond a regression test. The implementer answers this by reading `task_manager.py` and `mcp_server.py` at the relevant lines and documenting the answer in the commit message.

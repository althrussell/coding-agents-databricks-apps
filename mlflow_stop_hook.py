"""Claude Code ``Stop`` hook entrypoint for MLflow tracing.

Wired by ``setup_mlflow.py``. This wrapper exists so the hook is *import-safe*:
it must never fail a Claude Code turn. Two reasons it could otherwise blow up
on every turn:

  * The app's default dependency is ``mlflow-skinny``, which does NOT ship the
    ``mlflow.claude_code`` integration — so ``from mlflow.claude_code.hooks
    import stop_hook_handler`` raises ``ModuleNotFoundError``.
  * Tracing is opt-in (``MLFLOW_TRACING_ENABLED`` defaults to false), so most
    deployments never install full ``mlflow`` at all.

Inlining that import directly in the hook command made every agent turn print
``Stop hook error: ... No module named 'mlflow'``. Here we swallow any import
failure and exit cleanly; when tracing IS enabled (full ``mlflow`` installed),
``stop_hook_handler`` itself short-circuits if the env flag is off.
"""

import sys


def main() -> int:
    try:
        from mlflow.claude_code.hooks import stop_hook_handler
    except Exception:
        # mlflow (or its claude_code integration) isn't available — nothing to
        # trace. A missing optional dependency must not surface as a hook error.
        return 0
    try:
        stop_hook_handler()
    except Exception:
        # Tracing is best-effort telemetry; never let it fail the agent turn.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Capture the attendee's OBO user token and pump it into agent CLI configs.

In OBO mode (``CODA_OBO_ENABLED``, lab only) the app reads
``x-forwarded-access-token`` off each authenticated inbound request and writes it
as the agent bearer using the same pipeline ``PATRotator`` uses. The agent then
acts AS the attendee. The token is short-lived (~60 min) and only refreshes on new
inbound requests, so a browser keepalive (see ``/api/obo-refresh``) keeps it fresh
while the tab is open.
"""

import os
import threading
import logging

from utils import ensure_https
from cli_auth import update_cli_tokens

logger = logging.getLogger(__name__)

HEADER = "x-forwarded-access-token"


class OBOTokenManager:
    """Holds the latest forwarded user token and pushes it to the agent CLIs.

    Thread-safe; a single instance is shared across the (single) gunicorn worker.
    Lab instances are single-user, so there is exactly one user's token to track.
    """

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

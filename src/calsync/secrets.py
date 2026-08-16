"""Resolve a source's ``secret_ref`` to an actual credential.

Secrets are deliberately absent from the database. ``sources.url_template``
stores ``{{secret:p360_token}}``, never the token itself, so a source row can be
read, exported, backed up and pasted into a bug report without leaking a bearer
credential.

Lookup order is environment first (``CALSYNC_SECRET_P360_TOKEN``), then a JSON
file. Environment wins so one value can be overridden for a single run without
editing the file.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

ENV_PREFIX = "CALSYNC_SECRET_"
DEFAULT_FILE = Path.home() / ".config" / "calsync" / "secrets.json"


class SecretError(RuntimeError):
    """A referenced secret could not be resolved.

    Never carries the secret value — this message ends up in logs and in the
    ``sources.last_error`` column.
    """


def env_name(ref: str) -> str:
    """``p360_token`` -> ``CALSYNC_SECRET_P360_TOKEN``."""
    return ENV_PREFIX + ref.upper().replace("-", "_")


class SecretStore:
    def __init__(self, *, path: str | Path | None = None, environ: dict | None = None):
        self._environ = os.environ if environ is None else environ
        self.path = Path(path) if path is not None else DEFAULT_FILE
        self._cache: dict[str, str] | None = None

    def _from_file(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        if not self.path.exists():
            self._cache = {}
            return self._cache

        # A file of bearer tokens that other local accounts can read is a
        # credential leak, and the fix is one command — so refuse rather than
        # warn and carry on.
        mode = self.path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            raise SecretError(
                f"{self.path} is readable by group or others; run: chmod 600 {self.path}"
            )

        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            raise SecretError(f"could not read secrets from {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise SecretError(f"{self.path} must contain a JSON object of ref -> secret")

        self._cache = {str(k): str(v) for k, v in data.items()}
        return self._cache

    def get(self, ref: str) -> str:
        value = self._environ.get(env_name(ref))
        if value:
            return value
        value = self._from_file().get(ref)
        if value:
            return value
        raise SecretError(
            f"no secret for {ref!r}; set {env_name(ref)} or add it to {self.path}"
        )

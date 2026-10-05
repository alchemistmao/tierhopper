"""Where TierHopper keeps its files, and which mode it runs in.

Local mode (default): state in one SQLite file, results fetched straight from the provider. Nothing
to set up beyond one provider account.
Full mode: state in your own Supabase project, checkpoints in your own R2 bucket and a scheduler on
your own Modal account, enabled by `tierhopper config supabase` / `config r2` / `control deploy`.
"""

from __future__ import annotations

import os
from pathlib import Path

from tierhopper import credentials


def home() -> Path:
    """State directory: $TIERHOPPER_HOME or ~/.tierhopper."""
    return Path(os.environ.get("TIERHOPPER_HOME") or Path.home() / ".tierhopper").expanduser()


def results_dir() -> Path:
    return home() / "results"


def packages_dir() -> Path:
    return home() / "packages"


def database_path() -> Path:
    return home() / "state.db"


def full_mode() -> bool:
    """True once a Supabase project is configured (URL + secret key)."""
    return bool(credentials.get_secret("supabase", "url") and credentials.get_secret("supabase", "secret_key"))


def mode() -> str:
    return "full" if full_mode() else "local"

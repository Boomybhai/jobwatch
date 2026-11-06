"""Where jobwatch's files live, both as a Python project and as a .exe."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Folder holding config.yaml, companies.csv, state.json and email.env.

    For the .exe that's the folder the .exe sits in, so you can edit the files
    next to it. For the Python project it's the project root.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> list[str]:
    """Read KEY=value lines (e.g. email.env) into os.environ.

    Real environment variables win, so GitHub Actions secrets still apply.
    Returns the keys that were set.
    """
    if not path.exists():
        return []
    loaded = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and not os.environ.get(key):
            os.environ[key] = value
            loaded.append(key)
    return loaded

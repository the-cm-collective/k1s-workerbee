#!/usr/bin/env python3
"""Move bundled hyperon-das wheel dependencies onto Python's import path."""

from __future__ import annotations

import shutil
import site
from pathlib import Path

base = Path(site.getsitepackages()[0])
nested = base / "site-packages"
if nested.exists():
    for child in nested.iterdir():
        destination = base / child.name
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        shutil.move(str(child), str(destination))
    nested.rmdir()

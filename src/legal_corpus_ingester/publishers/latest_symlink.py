from __future__ import annotations

import os
from pathlib import Path


def atomic_symlink(target_name: str, link_path: Path) -> None:
    """Atomically point *link_path* -> *target_name* (relative symlink).

    Uses a tmp symlink in the same parent directory + ``os.rename`` so
    the swap is visible as a single filesystem operation.
    """
    tmp = link_path.parent / f".tmp_link_{os.getpid()}"
    # Remove any leftover tmp from a previous crashed run
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(target_name)
    os.rename(str(tmp), str(link_path))

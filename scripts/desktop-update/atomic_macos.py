#!/usr/bin/env python3
"""On-demand atomic coordinator for the macOS Hermes desktop updater."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
from typing import Union


PathLike = Union[str, os.PathLike]

_AT_FDCWD = -2
_RENAME_SWAP = 0x00000002


def atomic_exchange(left: PathLike, right: PathLike) -> None:
    """Atomically exchange two existing names with macOS ``RENAME_SWAP``."""
    if sys.platform != "darwin":
        raise OSError("atomic path exchange is supported only on macOS")

    left_path = Path(left)
    right_path = Path(right)
    if left_path.is_symlink() or right_path.is_symlink():
        raise ValueError("atomic exchange endpoints must not be symlinks")
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = libc.renameatx_np
    renameatx_np.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameatx_np.restype = ctypes.c_int

    result = renameatx_np(
        _AT_FDCWD,
        os.fsencode(left_path),
        _AT_FDCWD,
        os.fsencode(right_path),
        _RENAME_SWAP,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            f"{left_path} <-> {right_path}",
        )


if __name__ == "__main__":
    raise SystemExit("atomic coordinator command interface is not implemented yet")

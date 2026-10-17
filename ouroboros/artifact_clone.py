"""Clone-on-copy for task artifact files: a new inode sharing the source's blocks where the filesystem
can give one (macOS APFS ``clonefile(2)``, Linux ``FICLONE`` on Btrfs and reflink-enabled XFS); any
other platform, filesystem or volume boundary answers ``False`` and the caller streams a copy as before.
Never a hard link: an in-place write to the source would change the stored result, while a clone
diverges at the first write to either side. The platform calls are function-local under ``sys.platform``
guards (docs/development/10-platform-abstraction-rule.md names this module's exception)."""
from __future__ import annotations

import os
import pathlib
import sys

_FICLONE = 0x40049409  # _IOW(0x94, 9, int), <linux/fs.h>


def clone_file(source: pathlib.Path, destination: pathlib.Path) -> bool:
    """Make DESTINATION (which must not exist) a block-sharing clone of SOURCE; ``False`` when this
    platform or filesystem cannot, with nothing left at DESTINATION, so the caller copies instead."""
    source, destination = pathlib.Path(source), pathlib.Path(destination)
    try:
        if sys.platform == "darwin":
            return _clonefile(source, destination)
        if sys.platform.startswith("linux"):
            return _ficlone(source, destination)
    except OSError:
        return False
    return False


def _clonefile(source: pathlib.Path, destination: pathlib.Path) -> bool:
    import ctypes

    try:
        clonefile = ctypes.CDLL(None, use_errno=True).clonefile
    except (OSError, AttributeError):
        return False
    clonefile.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)
    clonefile.restype = ctypes.c_int
    # A refusal (another volume, a non-APFS filesystem, an unreadable source) leaves no file behind:
    # the streamed copy then succeeds or reports the real fault with its own message.
    return clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0


def _ficlone(source: pathlib.Path, destination: pathlib.Path) -> bool:
    import fcntl

    try:
        with open(source, "rb") as reader, open(destination, "xb") as writer:
            try:
                fcntl.ioctl(writer.fileno(), _FICLONE, reader.fileno())
                return True
            except OSError:
                pass
    except OSError:
        return False
    destination.unlink(missing_ok=True)  # the empty file of a refused clone
    return False

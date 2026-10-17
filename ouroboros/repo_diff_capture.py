"""Git's own retargeting variables, removed from a Git child process's environment.

``GIT_DIR`` and its siblings would make a Git command read a FOREIGN repository
(or turn a plain folder into one), so a caller that judges exactly one selected
root strips them and lets that root alone decide. Read by the reverse patch check
in ``tools.subagent_integration``.
"""

from __future__ import annotations

_GIT_RETARGET_ENV = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
})

"""Invoke entry point that exposes the playground workflow.

This module lives at the repository root so ``uv run invoke
playground.<task>`` resolves the named collection built from the tasks
exported by :mod:`src.workflows.playground`.
"""

from invoke import Collection, Task

from src.workflows import playground


def _build_collection() -> Collection:
    """Compose the ``playground`` namespace from the workflow tasks."""
    inner = Collection("playground")
    for name in ("up", "push", "check", "down", "reset"):
        inner.add_task(getattr(playground, name), name)
    outer = Collection()
    outer.add_collection(inner)
    return outer


ns = _build_collection()

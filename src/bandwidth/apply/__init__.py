"""Applying to jobs: Partial help fills a form and hands it over; Fast apply submits after a tap (SPEC §10, §11)."""

from __future__ import annotations

from pathlib import Path


def applyable_platforms(vault_dir: Path | None = None) -> tuple[str, ...]:
    """The platforms whose forms can be filled now: none until the vault exists (JOB-4, VLT-3)."""
    from .forms import PAGE_READERS, READERS
    from .vault import VAULT_DIR

    return (*READERS, *PAGE_READERS) if ((vault_dir or VAULT_DIR) / "profile.yaml").exists() else ()

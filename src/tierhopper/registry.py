"""Provider registry: `providers.yaml` is the seed; the store holds the live status."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from tierhopper.models import Provider
from tierhopper.store.base import NotFound, Store

DEFAULT_REGISTRY = Path(os.environ.get("TIERHOPPER_REGISTRY") or Path(__file__).resolve().parent / "providers.yaml")


def load_registry(path: Path = DEFAULT_REGISTRY) -> list[Provider]:
    data = yaml.safe_load(path.read_text()) or {}
    entries = data.get("providers", {})
    return [Provider.model_validate({"id": pid, **fields}) for pid, fields in entries.items()]


def sync_registry(store: Store, path: Path = DEFAULT_REGISTRY) -> list[Provider]:
    """Insert new providers and refresh static fields, keeping the live `status` from the store."""
    synced = []
    for seed in load_registry(path):
        try:
            current = store.get_provider(seed.id)
        except NotFound:
            synced.append(store.upsert_provider(seed))
            continue
        seed = seed.model_copy(update={"status": current.status, "updated_at": current.updated_at})
        if seed.model_dump() == current.model_dump():
            synced.append(current)  # nothing changed: do not rewrite the row (a status change may be landing)
            continue
        synced.append(store.upsert_provider(seed))
    return synced

from tierhopper.store.base import NotFound, Store
from tierhopper.store.memory import MemoryStore


def open_store() -> Store:
    """The store for this machine: your Supabase project in full mode, a local SQLite file otherwise."""
    from tierhopper import config

    if config.full_mode():
        from tierhopper.store.supabase_store import SupabaseStore

        return SupabaseStore()
    from tierhopper.store.sqlite_store import SQLiteStore

    return SQLiteStore(config.database_path())


__all__ = ["MemoryStore", "NotFound", "Store", "open_store"]

"""Provider adapters, looked up by the `adapter` field of the registry."""

from __future__ import annotations

from tierhopper.adapters.base import AdapterError, ProviderAdapter


def get_adapter(name: str) -> ProviderAdapter:
    if name == "modal":
        from tierhopper.adapters.modal_adapter import ModalAdapter

        return ModalAdapter()
    if name == "kaggle":
        from tierhopper.adapters.kaggle_adapter import KaggleAdapter

        return KaggleAdapter()
    if name == "lightning":
        from tierhopper.adapters.lightning_adapter import LightningAdapter

        return LightningAdapter()
    if name == "runpod":
        from tierhopper.adapters.runpod_adapter import RunPodAdapter

        return RunPodAdapter()
    raise AdapterError(f"no adapter named {name!r}")


IMPLEMENTED = {"modal", "kaggle", "lightning", "runpod"}

__all__ = ["IMPLEMENTED", "AdapterError", "ProviderAdapter", "get_adapter"]

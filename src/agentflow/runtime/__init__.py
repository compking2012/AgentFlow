"""Controller-owned process lifecycle and execution boundaries."""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contracts import BackendHandle, Capability, LaunchSpec, TaskEnvelope

__all__ = ["BackendHandle", "Capability", "LaunchSpec", "TaskEnvelope"]


def __getattr__(name: str):
    # `python -m agentflow.runtime.launcher` must not initialize Pydantic schemas.
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import contracts

    value = getattr(contracts, name)
    globals()[name] = value
    return value

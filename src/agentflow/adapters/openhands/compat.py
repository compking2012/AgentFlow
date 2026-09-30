"""Local compatibility for the locked SDK/LiteLLM response representation."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from litellm.types.utils import ModelResponse


def normalize_optional_chat_usage(response: ModelResponse) -> ModelResponse:
    """Repair one phantom field marker without inventing usage or changing JSON.

    LiteLLM 1.101.0 assigns then deletes a missing cache_creation_tokens value,
    leaving its name in Pydantic's fields_set. OpenHands 1.49.2 trusts that marker
    during telemetry and directly reads the deleted attribute. Clear only that
    inconsistent marker on copied objects; provider counts and reported cache
    values remain intact. The controller's original receipts/ledger are separate.
    """
    from litellm.types.utils import PromptTokensDetailsWrapper, Usage

    usage = getattr(response, "usage", None)
    if not isinstance(usage, Usage):
        return response
    details = getattr(usage, "prompt_tokens_details", None)
    if (not isinstance(details, PromptTokensDetailsWrapper)
            or "cache_creation_tokens" not in details.model_fields_set
            or hasattr(details, "cache_creation_tokens")):
        return response
    normalized_details = details.model_copy()
    normalized_details.__pydantic_fields_set__.discard("cache_creation_tokens")
    normalized_usage = usage.model_copy(update={"prompt_tokens_details": normalized_details})
    return response.model_copy(update={"usage": normalized_usage})

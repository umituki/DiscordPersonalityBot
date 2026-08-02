"""LLM boundary.

Spec 2.1: the LLM is not the authority on personality or database state. This
package is a *transport and validation* layer: it produces validated candidate
structures. Whether a candidate becomes state is decided by the owning engine,
the arbitrator and the committer — never here.

Rules (`.claude/rules/llm.md`):

* all model access goes through :class:`LLMClient`,
* structured output is schema-validated before use,
* invalid output must not update state (spec 28.2),
* model and prompt versions are recorded for important calls (spec 29).
"""

from app.llm.client import LLMClient, TracedLLMClient
from app.llm.errors import (
    LLMError,
    LLMParseError,
    LLMProtocolError,
    LLMSchemaError,
    LLMTimeoutError,
    LLMTransportError,
    LLMUnavailableError,
)
from app.llm.ollama import OllamaClient
from app.llm.prompts import PromptRegistry, PromptTemplate
from app.llm.structured import StructuredGenerator, StructuredOutcome
from app.llm.types import LLMMessage, LLMRequest, LLMResponse
from app.llm.validation import (
    Stage,
    ValidationContext,
    ValidationFailure,
    ValidationPipeline,
    Validator,
)

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMMessage",
    "LLMParseError",
    "LLMProtocolError",
    "LLMRequest",
    "LLMResponse",
    "LLMSchemaError",
    "LLMTimeoutError",
    "LLMTransportError",
    "LLMUnavailableError",
    "OllamaClient",
    "PromptRegistry",
    "PromptTemplate",
    "Stage",
    "StructuredGenerator",
    "StructuredOutcome",
    "TracedLLMClient",
    "ValidationContext",
    "ValidationFailure",
    "ValidationPipeline",
    "Validator",
]

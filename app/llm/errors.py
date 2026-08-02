"""LLM failure taxonomy (spec 28.1).

Each error maps to a ``failures.failure_type`` so that a bad generation is
recorded as data, not just logged and lost.
"""

from __future__ import annotations


class LLMError(RuntimeError):
    """Base class for every LLM boundary failure."""

    failure_type = "generation"
    reason_code = "llm_error"
    retryable = False


class LLMTransportError(LLMError):
    """The model host could not be reached or returned a transport error."""

    failure_type = "transport"
    reason_code = "llm_transport_error"
    retryable = True


class LLMTimeoutError(LLMTransportError):
    """The model did not answer within the configured timeout."""

    reason_code = "llm_timeout"
    retryable = True


class LLMUnavailableError(LLMTransportError):
    """The model host is up but the requested model is not usable."""

    reason_code = "llm_unavailable"
    retryable = False


class LLMProtocolError(LLMError):
    """The host answered with a body that is not a valid API response."""

    failure_type = "parse"
    reason_code = "llm_protocol_error"
    retryable = True


class LLMParseError(LLMError):
    """The generated text is not parseable as the requested format."""

    failure_type = "parse"
    reason_code = "llm_parse_error"
    retryable = True


class LLMSchemaError(LLMError):
    """The parsed output does not satisfy the requested schema."""

    failure_type = "schema"
    reason_code = "llm_schema_error"
    retryable = True


class LLMValidationError(LLMError):
    """The output is well-formed but semantically unacceptable."""

    failure_type = "semantic"
    reason_code = "llm_semantic_rejected"
    retryable = False

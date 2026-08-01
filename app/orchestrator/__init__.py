"""Orchestration layer.

Spec 7.1: the orchestrator owns processing order. It makes no psychological
judgement of its own.
"""

from app.orchestrator.processor import EventProcessor, ProcessingOutcome
from app.orchestrator.run_context import RunContext

__all__ = ["EventProcessor", "ProcessingOutcome", "RunContext"]

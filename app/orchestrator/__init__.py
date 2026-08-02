"""Orchestration layer.

Spec 7.1: the orchestrator owns processing order. It makes no psychological
judgement of its own.

Import from submodules directly — ``app.events.bus`` depends on
``app.orchestrator.run_view``, so eager re-exports here would create an import
cycle through the processor.
"""

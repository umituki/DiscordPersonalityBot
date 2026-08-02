"""Development-only conversation evaluation (patch spec 11).

Nothing in this package may be imported by the running system. Patch spec
prohibition 10 is explicit: ``外部LLMを本番全replyの必須依存にしない`` — an
evaluator that production depends on is exactly that dependency, whichever
provider it happens to use. It is enforced by
``tests/invariants/test_architecture_boundaries.py``.
"""

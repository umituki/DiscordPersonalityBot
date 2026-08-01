---
paths:
  - "tests/**/*.py"
  - "app/**/*.py"
---

# Testing rules

- Use pytest.
- Unit tests must use temporary databases and isolated filesystem paths.
- Tests must never open the production database.
- Add regression coverage for every fixed invariant violation.
- Prefer state/result assertions over exact generated prose assertions.
- LLM-dependent tests should mock the LLM boundary unless the test explicitly targets Ollama integration.
- Scenario tests should check invariants and allowed ranges, not one exact psychological number.
- Failure tests must verify that invalid proposals are rejected and partial state is not committed.

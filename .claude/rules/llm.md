---
paths:
  - "app/llm/**/*.py"
  - "app/conversation/**/*.py"
  - "config/prompts/**/*"
---

# LLM rules

- All model access goes through `LLMClient`.
- Structured outputs require schema validation before use.
- LLM confidence is only one signal; it is not authoritative confidence.
- LLM output must never directly set database personality, relationship, belief, or memory state.
- Separate appraisal/meaning proposals from Python-owned update rules.
- Record model version and prompt version for important structured calls.
- Output Guard must reject impossible real-world physical claims by YUI unless clearly framed as virtual.
- Search/tool claims may only be stated as successful when Tool Manager reports success.

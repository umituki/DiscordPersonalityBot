---
paths:
  - "app/storage/**/*.py"
  - "app/state/**/*.py"
  - "app/events/**/*.py"
---

# Storage / state rules

- SQLite dynamic data changes require migrations.
- Events are immutable by default; corrections create new invalidation/reinterpretation events.
- Use timezone-aware timestamps.
- Write multi-domain state atomically.
- Do not clamp obviously invalid state proposals into valid ranges; reject and record the failure.
- Delivery processing must be idempotent.
- Keep schema versions for event payloads that may evolve.
- Do not use JSON blobs as an excuse to avoid typed domain boundaries.

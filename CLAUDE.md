# YUI v2 — Claude Code Project Instructions

## Source of truth

- Canonical specification: `docs/YUI_v2_SPEC.md`.
- Before implementing a subsystem, read the relevant specification section.
- Do not silently change the specification to fit existing code. If a spec change is required, explain the conflict and ask the owner.
- v1 code is reference material only. Do not preserve its architecture unless the v2 spec explicitly requires it.

## Core invariants

- Python/SQLite own authoritative state. The LLM never directly mutates state.
- Objective world/archive and subjective memory/psychology are separate.
- Every dynamic state domain has a single writer.
- Cross-domain effects are Evidence or StateChangeProposal objects, then validation/arbitration/transaction.
- Meaningful state changes must be traceable to events/provenance.
- Never treat a plan as a completed experience.
- Never mix USER, NPC, simulated past, real Discord history, or admin operations.
- Never give past simulation knowledge that was unavailable at the simulated time.
- Personality and values must not change directly from a single event.
- Tool success is authoritative only when Tool Manager reports success.
- Invalid LLM output must not be committed.

## Development order

Implement only the current roadmap phase unless explicitly asked otherwise:

1. Foundation
2. Persistence / Event / State
3. Ollama structured LLM
4. Basic Discord conversation
5. Context / Episode / Memory
6. Immediate psychology
7. Social / Relationship / Belief / Self
8. Agency / Goals / Habits / Epistemics
9. Virtual life / Sleep / Scheduler
10. Consolidation / Personality / Values
11. NPC society
12. Historical knowledge / Past simulation
13. Production hardening

Do not implement later-phase shortcuts inside earlier phases.

## Change discipline

- Keep each change focused on one responsibility.
- Inspect existing interfaces before editing.
- Prefer root-cause fixes over special-case conditionals.
- Public/domain boundaries should be typed.
- Do not spread raw SQL outside the storage/repository layer.
- Do not scatter prompt strings through business logic; prompts must be versionable.
- Do not add a dependency without explaining why it is required.
- Do not introduce a vector DB before FTS5 has been implemented and measured.

## Testing

Every implementation task must include relevant tests.

At minimum protect:

- event immutability
- idempotent delivery
- single-writer ownership
- transaction rollback
- restart/recovery behavior
- USER/NPC separation
- plan/completed separation
- objective archive/subjective memory separation
- temporal knowledge guard

Never weaken an invariant test merely to make a change pass.

## Data safety

Do not edit, delete, commit, or use as test fixtures:

- `.env`
- production `data/`
- `backups/`
- private logs/userdata
- production SQLite/WAL/SHM files

Use temporary/test databases for tests and simulations.

Before any destructive schema/data operation, require an explicit owner request and a recoverable snapshot/backup path.

## Completion report

After a coding task, report:

1. files changed
2. behavior implemented
3. tests run and result
4. invariants affected
5. remaining TODOs / next roadmap task

Do not claim completion if tests were not run or if required behavior is stubbed.

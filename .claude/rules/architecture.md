# Architecture rules

- `app/interfaces/` converts external input/output; it does not own psychology or persistence rules.
- `app/orchestrator/` owns processing order, not domain judgment.
- `app/events/` stores and routes events; it does not decide psychology.
- `app/state/` owns proposals, dependency validation, arbitration, atomic commit.
- Domain engines may read other domains where allowed but may only write their owned state.
- Cross-domain updates must be emitted as evidence/proposals.
- `app/storage/` and repositories are the only normal SQL boundary.
- `app/tools/` owns external tool execution truth.
- `app/context/` selects context and must not mutate state as a side effect.
- Past simulation reuses the normal event/psychology/memory pipeline; do not create a second simplified personality engine.

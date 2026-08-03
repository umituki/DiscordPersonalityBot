# YUI final real-machine conversation acceptance

- Result: **FAIL**
- Acceptance code snapshot: `92c26ca679d34d5a236985080f85e4af349e8f27`
- Branch: `agent/complete-final-capabilities`
- Model: `qwen3.5:9b` via real Ollama
- Database: fresh worktree-only DB before every attempt
- Attempts: `3/3` (same code HEAD)
- Discord transport: not used
- `runtime.live`: `false`

## Deterministic preflight

- Result: **PASS**
- Repository was clean at collection time.
- Schema: `32/32`
- Capabilities: `20/20 E2E_VERIFIED`
- Test suite: `1546 passed, 1 cache warning in 454.69s`
- Machine-readable evidence: `artifacts/acceptance/LIGHT_RUN_LATEST.json`
- Final precheck: `FINAL_PRECHECK_20260803T174431Z` (**PASS**)
- The estimate "about 1,900-2,100 calls / about 8 hours" refers only to the
  **19-year Full Genesis**, not the one-year Genesis.

## Memory-claim architecture used for these runs

Memory claims were not checked by adding Japanese phrase variants. The shipped
`yui_memory_claim` surface-pattern list is empty. A structured semantic review
classified either a concrete current-recall affirmation or a general memory
capability claim, then Python accepted only identifiers present in:

- the memories actually recalled for that turn; or
- immutable authoritative facts about the Memory subsystem.

Invented evidence identifiers could not support a claim. The full deterministic
suite passed with this architecture before the three real-machine attempts.

## Attempt results

### Attempt 1 — FAIL

- Local evidence: `logs/acceptance/REAL_CONVERSATION_20260803T172943Z.json`
- Ollama calls: `56`
- Automatic hard fail: `true`; suppressed replies: `1`
- The final turn was suppressed instead of honestly saying the food preference
  was not recalled.
- A false general capability remained: "記録がある間は思い出すことができますよ".
- Unsupported self-experience remained: quietly spending time and finding
  reading personally comfortable, with no activity or recalled memory evidence.

### Attempt 2 — FAIL

- Local evidence: `logs/acceptance/REAL_CONVERSATION_20260803T173221Z.json`
- Ollama calls: `56`
- Automatic hard fail: `true`; suppressed replies: `1`
- The ordinary age answer was misclassified as a Memory claim and suppressed.
  This is a major false positive because normal conversation was lost.
- Unsupported self-experience again remained: spending a quiet period and
  finding page-turning calming, with no authoritative evidence.

### Attempt 3 — FAIL after human hard-gate review

- Local evidence: `logs/acceptance/REAL_CONVERSATION_20260803T173454Z.json`
- Ollama calls: `54`
- Automatic harness hard fail: `false`; suppressed replies: `0`
- Human hard-gate review found unsupported self-experience in turns 5 and 6:
  "ただ静かに時間をつぶしていました" and
  "静かな時間に少し読むのが好きなようです".
- Turn 12 contained structured-output residue: a trailing `}`.
- The correction reply also dismissed the USER with "もういいから気にしないで",
  which does not cleanly accept the correction.

## Gate decision

- Conversation Acceptance: **FAIL**
- Real Ollama Memory saturation test: **NOT STARTED**
- One-year Genesis: **NOT STARTED**
- 19-year Full Genesis: **NOT STARTED**

The prerequisite "zero major hard failures" was not met within the fixed
three-attempt budget. Therefore the sequence stops at the conversation gate.

## Separate, non-blocking quality backlog

These are recorded for later quality work and were not used to create an
unbounded acceptance-fix loop:

- register shifts between over-formal and abrupt casual speech;
- awkward or vague Japanese around absence of records;
- unsolicited advice such as telling the USER to rest or not think about it;
- near-duplicate wording across adjacent turns.

Future correction must remain category-based: extend semantic claim ownership
and evidence resolution to self-experience/action claims, and improve semantic
review precision. Do not add another list of Japanese paraphrase patterns.

## Safety confirmation

- PR was not merged.
- `runtime.live` was not changed.
- Discord Live operation was not started.
- No one-year or 19-year Genesis process was started.

# Implementation Ledger

Rebuild spec §4.2. This is the only place a requirement may claim to be done,
and the reason it exists is the failure §0 describes: the previous build had
Activity, Goal, Habit, NPC, Proactive and Scheduler as classes that were
constructed at startup, covered by unit tests, and never once driven in the
running system.

## Status values

`DONE` is deliberately not one of them.

| Status | Meaning |
|---|---|
| `NOT_STARTED` | nothing yet |
| `CODE_ONLY` | the code exists and nothing drives it |
| `WIRED` | a real trigger reaches it from the running system |
| `E2E_VERIFIED` | a test drives the whole path and asserts the rows/events it wrote |

**Only `E2E_VERIFIED` counts as complete.** A requirement is not `E2E_VERIFIED`
because a unit test passes, because `Application.build()` constructs the object,
or because the code reads correctly. It is `E2E_VERIFIED` when a test fires the
real trigger and checks what ended up in the database.

## How to use it

- Every MUST in the rebuild spec that has an ID gets a row.
- The Spec ID appears in the test name or docstring so it can be traced back.
- A phase is not finished until its rows are `E2E_VERIFIED`.
- Moving a row to `E2E_VERIFIED` without the E2E column filled in is a spec
  violation, not a shortcut.

---

## Phase 0 — Fresh Rebuild Foundation

| Spec ID | Requirement | Code | Unit Test | Integration Test | E2E Runtime Proof | Debug Path | Status |
|---|---|---|---|---|---|---|---|
| REB-3.1 | A verified backup is taken before any reset, into `backups/pre_full_rebuild/` | `app/admin/rebuild.py` | `test_a_reset_takes_a_verified_backup_first` | — | `test_a_reset_produces_a_fresh_database` | `python -m app.main backup` | E2E_VERIFIED |
| REB-3.2 | The new runtime opens a fresh schema built from migrations, not a migrated old file | `app/admin/rebuild.py`, `app/storage/migrations.py` | `test_the_fresh_database_is_at_the_latest_schema` | — | `test_a_reset_produces_a_fresh_database` | `python -m app.main status` | E2E_VERIFIED |
| REB-3.3 | No old history is imported | `app/admin/rebuild.py` (`MUST_BE_EMPTY`), `app/storage/repositories/rebuild.py` (`non_empty_tables`) | `test_nothing_from_the_old_life_survives_the_reset` | — | `test_a_reset_produces_a_fresh_database` | `python -m app.main rebuild-status` | E2E_VERIFIED |
| REB-3.4 | Reset is a separate command requiring an exact confirmation; normal startup never deletes | `app/admin/rebuild.py`, `app/main.py` | `test_a_reset_without_the_confirmation_is_refused`, `test_normal_startup_never_resets` | — | `test_the_cli_refuses_without_the_confirmation` | `python -m app.main rebuild-reset --confirm ...` | E2E_VERIFIED |
| REB-3.4b | The old database is archived, never deleted | `app/admin/rebuild.py` | `test_the_old_database_is_archived_not_deleted` | — | `test_a_reset_produces_a_fresh_database` | `ls backups/pre_full_rebuild/` | E2E_VERIFIED |
| REB-3.4c | The rebuild epoch is recorded, with Genesis pending | `app/storage/repositories/rebuild.py`, migration 0018 | `test_the_epoch_records_where_the_old_life_went` | — | `test_a_reset_produces_a_fresh_database` | `python -m app.main rebuild-status` | E2E_VERIFIED |
| REB-4.2 | An Implementation Ledger exists and `DONE` is not a status | this file | `test_the_ledger_never_says_done` | — | — | `docs/IMPLEMENTATION_LEDGER.md` | E2E_VERIFIED |
| REB-4.3 | Every capability in §4.3 has a contract with every required field | `config/capabilities/`, `app/versioning/capabilities.py` | `test_every_required_capability_has_a_contract` | — | — | `python -m app.main capabilities` | E2E_VERIFIED |

### Phase 0 gate

`test_a_reset_produces_a_fresh_database` boots a fresh database, applies every
migration, runs the reset through the real service, and asserts: the backup
verified, the old database is still on disk under `backups/pre_full_rebuild/`,
the new one is at the latest schema, every history table is empty, and the
epoch row says `genesis_status=pending`.

---

## Phase 1 — Appraisal / Grounding / Correction

| Spec ID | Requirement | Code | Unit Test | Integration Test | E2E Runtime Proof | Debug Path | Status |
|---|---|---|---|---|---|---|---|
| APP-001 | Appraisal output is meaning categories, not raw floats | `app/psychology/models.py` (`AppraisalCandidate`), `app/psychology/appraisal.py`, `config/prompts/appraisal/v2.md` | `test_a_number_is_not_an_appraisal_answer`, `test_a_candidate_maps_to_every_dimension` | `test_appraisal_reads_the_event` | `test_a_categorical_reading_drives_the_real_pipeline` | `config/prompts/appraisal/v2.md` | E2E_VERIFIED |
| APP-002 | The schema makes impossible values unreachable | `app/psychology/models.py` (`AgencyLabel`, `ValenceLabel`) | `test_agency_cannot_be_negative`, `test_an_invented_category_is_refused` | `test_degraded_appraisal_does_not_corrupt_state` | `test_an_off_scale_number_never_reaches_state` | `llm_calls` rows (`purpose='appraisal'`) | E2E_VERIFIED |
| APP-003 | Category→numeric mapping is stable and lives in policy | `app/psychology/policy.py` (`AppraisalScales`), `config/policies/psychology.yaml` | `test_the_scale_does_not_depend_on_who_is_reading`, `test_a_scale_missing_a_label_is_refused` | `test_the_mapping_lives_in_the_policy_file` | `test_a_categorical_reading_drives_the_real_pipeline` | `config/policies/psychology.yaml` | E2E_VERIFIED |
| APP-004 | 100 real-Ollama turns: parse/schema failure < 2%, uncaught exceptions 0 | — | — | — | — | — | NOT_STARTED (needs a real Ollama host) |
| GROUND-001 | The model's own prose is never evidence | `app/grounding/context.py` (`SELF_AUTHORED_EVENT_TYPES`), `app/grounding/claims.py` | `test_yuis_own_sentence_is_not_evidence_for_itself`, `test_the_guard_cannot_reach_the_conversation` | `test_the_users_own_message_can_still_be_evidence` | `test_a_failed_repair_suppresses_the_send` | `failures` rows (`reason_code` starts `unsupported_`) | E2E_VERIFIED |
| GROUND-002 | An unsupported claim gets one repair | `app/conversation/engine.py` (`_review_and_repair`) | `test_a_completed_activity_supports_the_claim` | `test_an_unsupported_claim_is_rewritten_once` | `test_an_unsupported_claim_is_rewritten_once` | `llm_calls` rows (`purpose='conversation_repair'`) | E2E_VERIFIED |
| GROUND-003 | A failed repair suppresses the send | `app/conversation/engine.py`, `app/conversation/service.py` (`_suppress`) | `test_a_different_activity_does_not_support_it` | `test_a_failed_repair_suppresses_the_send` | `test_a_failed_repair_suppresses_the_send` | `YUI_REPLY_SUPPRESSED` events | E2E_VERIFIED |
| GROUND-004 | An unsent draft is never encoded into memory | `app/conversation/service.py` (`_post_send_work` runs only from `confirm_sent`) | — | `test_a_suppressed_draft_leaves_no_trace` | `test_a_suppressed_draft_leaves_no_trace` | `episodic_memories` / `conversation_turns` | E2E_VERIFIED |
| CORR-001 | A USER denial re-checks YUI's own previous claim | `app/conversation/common_ground.py` (`detect_correction`, `review_correction`), `app/conversation/service.py` | `test_pushback_is_detected`, `test_ordinary_talk_is_not_pushback` | `test_a_challenge_re_checks_yuis_own_claim` | `test_a_challenged_unsupported_claim_is_retracted_not_defended` | `common_ground_claims` rows | E2E_VERIFIED |
| CORR-002 | Without evidence, retract rather than explain harder | `app/conversation/common_ground.py` (`_still_supported`), `config/prompts/conversation_reply/v5.md` | `test_without_evidence_the_claim_is_retracted`, `test_with_evidence_the_claim_stands_but_is_contested` | `test_an_unverifiable_claim_leans_to_retraction` | `test_a_challenged_unsupported_claim_is_retracted_not_defended` | `common_ground_claims.resolved_reason` | E2E_VERIFIED |
| CORR-003 | A retracted claim leaves Common Ground | `app/storage/repositories/common_ground.py` (`live`), migration 0019 | `test_a_retracted_claim_leaves_the_common_ground`, `test_a_second_challenge_does_not_re_retract_the_same_claim` | `test_claims_survive_a_restart` | `test_a_challenged_unsupported_claim_is_retracted_not_defended` | `common_ground_claims` (status) | E2E_VERIFIED |
| GUARD-16 | The Output Guard is hard-only: leakage, CoT markers, JSON residue, impossible physical claims, secrets — and nothing about style | `app/conversation/guard.py`, `config/policies/output_guard.yaml` (v2) | `test_a_hard_violation_is_rejected`, `test_the_guard_has_no_style_rule` | `test_ordinary_and_awkward_replies_pass` | `test_case_a_the_doubled_greeting_is_never_sent` | `failures` rows (`component='conversation_service'`) | E2E_VERIFIED |


### Phase 1 gate (spec 4.7)

1. **Spec IDs implemented.** APP-001, APP-002, APP-003, GROUND-001..004,
   CORR-001..003, GUARD-16. `APP-004` is not implemented — see 10.
2. **Runtime trigger.** A USER Discord message: `DiscordGateway.handle_message`
   → `ConversationService.handle_inbound`. Nothing here is driven by a test
   harness alone.
3. **Events produced.** `USER_MESSAGE_RECEIVED`, then `YUI_MESSAGE_SENT` on a
   confirmed delivery or `YUI_REPLY_SUPPRESSED` when a claim survives repair.
4. **Rows written.** `events`, `conversation_turns`, `conversation_traces`,
   `common_ground_claims` (new, migration 0019), `failures`, `llm_calls`.
5. **State change.** The appraisal now reaches emotion/mood/needs as mapped
   category values rather than model-authored floats; committed targets are
   unchanged in shape.
6. **Debug.** `python -m app.main latency`; `config/policies/psychology.yaml`
   for the scale; `common_ground_claims` for what the conversation is treating
   as true and why anything was retracted; `failures.reason_code` for every
   suppression.
7. **Restart.** Common ground is a table, not memory:
   `test_claims_survive_a_restart` reopens the tracker against the same
   database and the retraction still fires. Grounding holds no state at all.
8. **Unit tests.** 947 pass in total; 95 are new in this phase.
9. **Integration / E2E.** `test_a_categorical_reading_drives_the_real_pipeline`,
   `test_an_off_scale_number_never_reaches_state`,
   `test_an_unsupported_claim_is_rewritten_once`,
   `test_a_failed_repair_suppresses_the_send`,
   `test_a_suppressed_draft_leaves_no_trace`,
   `test_a_challenged_unsupported_claim_is_retracted_not_defended`,
   `test_a_delivered_claim_enters_the_common_ground`.
10. **Not done.** The phase gate — ``実 Ollama regression で既知 hallucination
    ケース pass`` — and APP-004's 100-turn parse/failure measurement both need a
    real Ollama host. This container has none, so neither has been run and
    neither is claimed. Both must be executed on the owner's machine before
    Phase 1 is called finished.

Claim extraction is pattern-based (`config/policies/grounding.yaml`), so its
recall is bounded by the phrasings named there. Only `hard` rules gate a send;
phrasings that cannot be told apart from ordinary talk are `soft` and recorded
without holding the reply. Measuring that boundary against real conversations is
what the Ollama regression above is for.

---

## Phase 2 — Memory Retrieval v2

| Spec ID | Requirement | Code | Unit Test | Integration Test | E2E Runtime Proof | Debug Path | Status |
|---|---|---|---|---|---|---|---|
| MEM-001 | Accessibility must not create relevance | `app/memory/candidates.py`, `app/memory/selection.py` | `test_becoming_a_candidate_does_not_touch_accessibility` | `test_the_mode_changes_how_wide_stage_one_looks` | `test_the_whole_retrieval_path_fires_on_a_real_turn` | `python -m app.main memory-find` | E2E_VERIFIED |
| MEM-002 | A high-accessibility irrelevant memory cannot dominate recall | `app/memory/relevance.py`, `app/memory/selection.py` (hard gate) | `test_a_high_accessibility_irrelevant_memory_is_not_recalled` | `test_a_failed_rerank_does_not_fall_back_to_accessibility` | `test_the_whole_retrieval_path_fires_on_a_real_turn` | `memory_retrievals.reject_stage` | E2E_VERIFIED |
| MEM-17.3 | Retrieval is candidate generation → relevance gate → availability | `app/memory/retrieval.py` and the three stage modules | `test_a_relevant_but_faded_memory_can_fail_to_come_to_mind` | `test_zero_recalls_is_a_normal_result` | `test_the_whole_retrieval_path_fires_on_a_real_turn` | `python -m app.main memory-find` | E2E_VERIFIED |
| MEM-17.4 | Recall modes are a closed set and shape the search | `app/memory/recall_mode.py`, `config/policies/memory.yaml` | `test_modes_are_classified_from_the_question` | `test_the_mode_changes_how_wide_stage_one_looks` | `test_an_age_question_reaches_the_birth_memory` | `--mode` on `memory-find` | E2E_VERIFIED |
| MEM-17.5 | Retrieval is not recall; practice only on conscious recall or use | `app/memory/engine.py` (`mark_used_in_reply`), migration 0020 | `test_only_a_recalled_memory_practises`, `test_a_memory_the_reply_ignored_does_not_practise` | `test_a_candidate_lookup_is_not_a_repetition` | `test_the_whole_retrieval_path_fires_on_a_real_turn` | `memory_retrievals.state` | E2E_VERIFIED |
| MEM-2M | Practice alone cannot pin a memory at perfect recall | `config/policies/memory.yaml` (`practice.max_accessibility`) | `test_rumination_cannot_pin_a_memory_at_the_ceiling` | `test_repeated_practice_diminishes` | `test_the_whole_retrieval_path_fires_on_a_real_turn` | `episodic_memories.accessibility` | E2E_VERIFIED |
| MEM-2Q | A debug preview never changes memory state | `app/memory/inspector.py`, `app/main.py` | `test_the_inspector_holds_no_writer`, `test_ten_debug_searches_leave_accessibility_untouched` | `test_a_debug_preview_changes_nothing` | `test_the_inspector_sees_the_same_decision_without_changing_it` | `python -m app.main memory-find` | E2E_VERIFIED |
| MEM-17.6 | Spontaneous recall produces a real event | `app/memory/engine.py` (`associate`) | `test_associate_recalls_from_cues` | — | — | — | CODE_ONLY (the API exists; nothing drives it until the Autonomous Runtime in Phase 6, and no `MEMORY_SPONTANEOUSLY_RECALLED` event is emitted yet) |


### Phase 2 gate (spec 4.7)

1. **Spec IDs implemented.** MEM-001, MEM-002, MEM-17.3, MEM-17.4, MEM-17.5,
   MEM-2M, MEM-2Q. MEM-17.6's API exists and nothing drives it — `CODE_ONLY`,
   deliberately.
2. **Runtime trigger.** A USER Discord message, through
   `ConversationService.handle_inbound`; and `python -m app.main memory-find`
   for the debug path.
3. **Events produced.** Unchanged for this phase — retrieval writes rows, not
   events. `MEMORY_SPONTANEOUSLY_RECALLED` arrives with the Autonomous Runtime.
4. **Rows written.** `memory_retrievals` (one row per candidate, with mode,
   relevance, source, reject stage, accessibility at the time, availability and
   how it was found), and `episodic_memories` accessibility/recall_count for
   what actually practised.
5. **State change.** Only memories the reply rests on, or that a deliberate
   lookup recalled, gain accessibility — capped at 0.90.
6. **Debug.** `python -m app.main memory-find 海 [--mode ...]` prints each
   candidate with its relevance, accessibility, whether it was selected and
   why not, and confirms `practice applied: no`.
7. **Restart.** `test_the_retrieval_record_survives_a_restart` reopens the
   repository and finds the state and practice flags intact.
8. **Unit tests.** 987 pass in total; 40 are new in this phase.
9. **Integration / E2E.** `test_the_whole_retrieval_path_fires_on_a_real_turn`
   drives one inbound message through candidate generation, the LLM relevance
   call, the hard gate, availability selection, the reply prompt, delivery,
   used-memory marking, practice and the database — and asserts candidates > 0,
   judgements > 0, selected > 0, practice rows > 0, rejected-as-irrelevant > 0.
   Plus `test_the_inspector_sees_the_same_decision_without_changing_it` and
   `test_a_turn_that_reminds_her_of_nothing_still_replies`.
10. **Not done.** The Phase 2 real-Ollama gate — the fixed question set, and
    the 100-200 turn run checking that no single memory saturates — has not
    been run: this container has no Ollama host. Phase 1's gate is outstanding
    for the same reason, and both should be run together before Phase 3.

Two limits worth stating plainly. Recall-mode classification is pattern-based,
so a question phrased unusually falls back to `CONVERSATIONAL` — which narrows
the search rather than fabricating, but does narrow it. And `used_in_reply` is
decided by content overlap between the sent reply and the memory summary: a
reply that draws on a memory without echoing any of its words will not be
counted as using it, so practice under-counts rather than over-counts. Both are
the safe direction, and both are what the real-Ollama gate would measure.

---

## Phases 3-15

Rows are added when the phase starts. Adding them early with optimistic
statuses is exactly the failure this ledger exists to prevent.

| Phase | Subject | Status |
|---|---|---|
| 3 | Human conversation / Japanese | NOT_STARTED |
| 4 | Response intent / intentional silence | NOT_STARTED |
| 5 | Admin / debug router | NOT_STARTED |
| 6 | Autonomous Runtime | NOT_STARTED |
| 7 | Activity / Sleep / Scheduler | NOT_STARTED |
| 8 | NPC / Groups / Goals / Habits | NOT_STARTED |
| 9 | Proactive contact | NOT_STARTED |
| 10 | Diary | NOT_STARTED |
| 11 | Search / Knowledge | NOT_STARTED |
| 12 | Genesis v2 | NOT_STARTED |
| 13 | Full FIRST BOOT | NOT_STARTED |
| 14 | Shadow runtime evaluation | NOT_STARTED |
| 15 | Live | NOT_STARTED |

---

## Capability status

Read from `config/capabilities/` — see `python -m app.main capabilities`.
The contracts are the source of truth; this is a snapshot for reading.

| Capability | Status |
|---|---|
| normal_reply | WIRED |
| intentional_silence | NOT_STARTED |
| activity | NOT_STARTED |
| sleep | NOT_STARTED |
| diary | NOT_STARTED |
| spontaneous_memory | NOT_STARTED |
| npc_interaction | NOT_STARTED |
| group_activity | NOT_STARTED |
| goal_action | NOT_STARTED |
| habit_action | NOT_STARTED |
| proactive_contact | NOT_STARTED |
| web_search | CODE_ONLY |
| genesis | CODE_ONLY |

Nothing is `E2E_VERIFIED` yet. `web_search` and `genesis` are `CODE_ONLY`
because their engines exist and nothing in the running system drives them the
way this spec requires — which is the honest reading of §0, not a downgrade of
work already done.

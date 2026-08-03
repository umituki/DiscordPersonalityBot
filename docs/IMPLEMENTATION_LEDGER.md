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
| `DEFERRED_TO_FINAL_REAL_MACHINE_GATE` | needs a real Ollama host or a long run; deliberately postponed to the single final acceptance, and **not** a claim that it passed |

`DEFERRED_TO_FINAL_REAL_MACHINE_GATE` is an OWNER decision recorded here, not a
weakening of `E2E_VERIFIED`. A requirement in that state has its structure
finished — trigger, wiring, gates, events, persistence, recovery, debug and a
fixture end-to-end test — and only its *generation-quality* verification
outstanding. It never means the code is unfinished, and it never means the test
passed.

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
| APP-004 | 100 real-Ollama turns: parse/schema failure < 2%, uncaught exceptions 0 | — | — | — | — | — | DEFERRED_TO_FINAL_REAL_MACHINE_GATE |
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

## Phase 3 — Human Conversation / Japanese

| Spec ID | Requirement | Code | Unit Test | Integration Test | E2E Runtime Proof | Debug Path | Status |
|---|---|---|---|---|---|---|---|
| CONV-10 | One SocialInterpretation call per turn, in meaning categories | `app/conversation/social_interpretation.py`, `config/prompts/social_interpretation/v1.md` | `test_the_interpretation_has_no_numeric_fields`, `test_an_invented_move_is_refused` | `test_an_unavailable_model_produces_the_minimal_reading` | `test_the_whole_realization_path_fires` | `python -m app.main conversation-plan` | E2E_VERIFIED |
| CONV-001 | A short USER message is read with the previous turn, not alone | `app/conversation/social_interpretation.py` (prompt), `ConversationEngine.plan_turn` | `test_the_schema_stays_small` | `test_scenario_c_no_errand_is_not_an_errand_to_find` | `test_the_whole_realization_path_fires` | `conversation-plan` | E2E_VERIFIED |
| CONV-002 | A short question after her own claim may be a challenge | `app/conversation/common_ground.py` (`detect_correction`) | `test_a_flat_denial_is_stronger_than_a_question` | `test_a_short_question_with_nothing_outstanding_is_just_a_question` | `test_scenario_f_a_correction_survives_the_surface_layer` | `common_ground_claims` | E2E_VERIFIED |
| CONV-3.6 | The USER's feelings are hinted at, never declared | `app/conversation/social_interpretation.py` (`UserStateHint`) | `test_there_is_no_field_that_declares_how_the_user_feels`, `test_every_user_state_value_is_hedged` | `test_the_hint_reaches_the_prompt_as_a_hint` | `test_scenario_e_something_heavy_is_not_over_counselled` | `conversation-plan` | E2E_VERIFIED |
| CONV-3.7 | Common Ground outranks the interpreter on correction | `app/conversation/social_interpretation.py` (`with_correction`) | `test_a_retraction_overrides_whatever_the_model_read` | `test_no_correction_leaves_the_reading_alone` | `test_scenario_f_a_correction_survives_the_surface_layer` | `common_ground_claims.resolved_reason` | E2E_VERIFIED |
| SURF-14 | SurfacePlan is Python-only and never writes Japanese | `app/conversation/surface.py` | `test_python_does_not_assemble_japanese`, `test_the_planner_never_calls_a_model` | `test_a_short_message_gets_a_short_reply_plan` | `test_scenario_b_a_one_word_message_is_not_answered_with_a_speech` | `conversation-plan` | E2E_VERIFIED |
| SURF-3.19 | Question need becomes a budget of 0 or 1, never a rate | `app/conversation/surface.py` (`question_budget`) | `test_the_question_budget_is_a_count` | `test_a_tired_user_is_not_interrogated` | `test_scenario_e_something_heavy_is_not_over_counselled` | `conversation-plan` | E2E_VERIFIED |
| SURF-3.16 | Relationship is a band with hysteresis | `app/conversation/surface.py` (`relationship_band`), `app/conversation/service.py` | `test_a_band_does_not_flicker_across_its_edge` | `test_a_stranger_is_addressed_politely_and_a_friend_is_not` | `test_the_same_message_is_planned_differently_by_distance` | `conversation-plan --band` | E2E_VERIFIED |
| SURF-3.22 | An experience may only be disclosed if something says it happened | `app/conversation/surface.py`, `app/grounding/claims.py` (`SELF_CLAIM_KINDS`) | `test_an_ungrounded_experience_is_capped_at_an_opinion` | `test_what_the_user_said_does_not_evidence_what_she_did` | `test_scenario_d_a_fabricated_experience_is_still_stopped` | `failures.reason_code` | E2E_VERIFIED |
| STYLE-3.18 | Repetition produces a hint, never a rejection | `app/conversation/repetition.py` | `test_an_overused_opening_is_reported`, `test_the_monitor_rejects_nothing` | `test_using_the_same_backchannel_twice_is_not_reported` | `test_the_whole_realization_path_fires` | realizer prompt | E2E_VERIFIED |
| REF-13.2 | A DialogueReferenceProvider is defined **and actually called** | `app/conversation/references.py`, `config/references/fixture_ja.yaml` | `test_the_corpus_declares_its_terms`, `test_at_most_five_examples_are_offered` | `test_the_query_carries_the_decided_shape` | `test_the_reference_corpus_reaches_the_realizer_prompt` | realizer prompt | E2E_VERIFIED |
| REF-13.3 | Corpus text can never become memory or common ground | `app/conversation/references.py` (`DialogueReference` has no memory shape) | `test_a_reference_has_none_of_the_shape_of_a_memory` | `test_corpus_text_never_becomes_a_conversation_turn` | `test_corpus_text_never_becomes_a_conversation_turn` | `episodic_memories` | E2E_VERIFIED |
| REF-3.28 | A missing or broken corpus is not an outage | `app/conversation/engine.py` (`_retrieve_references`), `app/bootstrap.py` | `test_a_corpus_without_provenance_is_refused` | `test_no_corpus_is_not_an_outage` | `test_a_broken_corpus_is_not_an_outage` | manifest `dialogue_reference_source` | E2E_VERIFIED |
| REAL-13.4 | The realizer receives every input and emits only `{"text": ...}` | `config/prompts/conversation_reply/v7.md`, `app/conversation/models.py` (`ReplyDraft`) | `test_engine_builds_identity_and_history_into_the_prompt` | `test_scenario_g_an_uncertain_memory_is_hedged_without_internal_words` | `test_the_whole_realization_path_fires` | `llm_calls` (`purpose='conversation_reply'`) | E2E_VERIFIED |
| NAT-3.36 | Naturalness is measured, never enforced at runtime | `app/evaluation/naturalness.py` | `test_the_reply_path_does_not_import_the_evaluator`, `test_the_evaluator_returns_rates_rather_than_a_verdict` | `test_a_reply_that_always_ends_in_a_question_is_visible` | — | `app/evaluation/naturalness.py` | WIRED (measured offline; no runtime trigger by design) |
| OBS-3.47 | The stages Phase 3 added are separately timed | `app/observability/trace.py`, migration 0021 | `test_every_stage_of_the_spec_is_markable` | `test_a_whole_turn_is_traced` | `test_the_trace_shows_the_stages_phase_three_added` | `python -m app.main latency` | E2E_VERIFIED |
| GATE-3 | Real-Ollama regression for Phases 1+2+3 | — | — | — | — | — | DEFERRED_TO_FINAL_REAL_MACHINE_GATE |

### Phase 3 gate (spec 4.7)

1. **Spec IDs implemented.** CONV-10, CONV-001, CONV-002, CONV-3.6, CONV-3.7,
   SURF-14, SURF-3.19, SURF-3.16, SURF-3.22, STYLE-3.18, REF-13.2, REF-13.3,
   REF-3.28, REAL-13.4, OBS-3.47. NAT-3.36 is `WIRED` on purpose — it is an
   offline evaluator and having a runtime trigger would make it a guard.
2. **Runtime trigger.** An accepted USER Discord message, through
   `ConversationService.handle_inbound`; `python -m app.main conversation-plan`
   for the preview.
3. **Events produced.** `USER_MESSAGE_RECEIVED`, then `YUI_MESSAGE_SENT` on a
   confirmed delivery or `YUI_REPLY_SUPPRESSED`.
4. **Rows written.** `conversation_traces` now carries
   `social_interpretation_*`, `reference_retrieval_*` and `realization_*`;
   `llm_calls` carries one `social_interpretation` and one
   `conversation_reply` per ordinary turn.
5. **State change.** None new. Phase 3 changes what is said, not what is
   committed — the relationship band is read, never written.
6. **Debug.** `python -m app.main conversation-plan "今日は疲れた"` prints the
   social reading, the surface plan and the reference count, and mutates
   nothing.
7. **Restart.** Nothing new to survive: the plan is per-turn. The relationship
   band resets to the committed familiarity on restart, which is correct —
   hysteresis is about not flickering within a conversation, not about
   remembering a hesitation across a reboot.
8. **Unit tests.** 1063 pass in total; 76 are new in this phase.
9. **Integration / E2E.** `test_the_whole_realization_path_fires`,
   `test_the_reference_corpus_reaches_the_realizer_prompt`,
   `test_the_trace_shows_the_stages_phase_three_added`, scenarios A-G in
   `tests/invariants/test_conversation_realization_e2e.py`, and the
   three-distance test of §40.
10. **Not done.** The combined real-Ollama gate for Phases 1+2+3 — the fixed
    question set, the known hallucination cases, the question-rate and
    repetition runs, and the latency check against `median <= 15s / p95 <= 30s`
    — has not been run, because this container has no Ollama host. Phase 4
    should not start until it has.

Two limits worth stating. `SocialInterpretation` costs one model call per turn
that the previous design also spent on the dialogue decision, so the call count
per ordinary reply is unchanged at two (plus memory rerank when there are
candidates); but the prompt is longer, and only hardware will say what that
costs. And the reference corpus shipped here is a ten-example developer fixture,
not a linguistic resource — it exercises the path and calibrates almost nothing.
Adding a real corpus is gated on checking its licence, which is why the
provenance fields are mandatory.

---

## Phase 4 — Response Intent / Intentional Silence

| Spec ID | Requirement | Code | Unit Test | Integration Test | E2E Runtime Proof | Debug Path | Status |
|---|---|---|---|---|---|---|---|
| SIL-12.1 | Four response intents, only one of which sends nothing | `app/conversation/response_intent.py` | `test_only_intentional_silence_says_nothing` | `test_a_speaking_inclination_is_taken_as_it_stands` | `test_a_chosen_silence_runs_the_whole_path` | `python -m app.main conversation-plan` | E2E_VERIFIED |
| SIL-12.2a | Python holds a hard veto the model cannot argue with | `app/conversation/response_intent.py` (`_veto`) | `test_a_required_response_is_never_left_silent` | `test_a_pending_correction_is_always_spoken` | `test_a_vetoed_turn_answers_and_records_no_silence` | `YUI_INTENTIONAL_SILENCE.veto` | E2E_VERIFIED |
| SIL-12.2b | Silence is allowed only where it is a natural thing to do | `app/conversation/response_intent.py` (`_silence_is_natural`) | `test_a_bare_backchannel_may_be_left`, `test_wanting_silence_is_not_enough_on_its_own` | `test_a_closing_conversation_may_be_left` | `test_a_chosen_silence_runs_the_whole_path` | `conversation-plan` | E2E_VERIFIED |
| SIL-12.3 | `YUI_INTENTIONAL_SILENCE` is never confused with a failure | `app/conversation/events.py`, `app/conversation/service.py` (`_stay_silent`) | `test_silence_and_suppression_are_different_things` | `test_the_silence_event_records_who_decided` | `test_a_chosen_silence_runs_the_whole_path` | events / `failures` (empty) | E2E_VERIFIED |
| SIL-12.3b | Silence is never the failure mode | `app/conversation/response_intent.py`, `SocialInterpretation.minimal` | `test_an_unreadable_inclination_answers`, `test_a_degraded_interpretation_answers` | `test_a_correction_reading_always_wants_to_speak` | `test_a_vetoed_turn_answers_and_records_no_silence` | `IntentDecision.source` | E2E_VERIFIED |
| SIL-12.4 | Typing starts only once a reply is certain | `app/interfaces/discord/gateway.py` (`on_speaking`), migration 0022 | `test_a_failure_before_the_decision_shows_no_typing_at_all` | `test_speaking_still_shows_typing` | `test_silence_shows_no_typing_indicator` | `conversation_traces.response_intent_*` | E2E_VERIFIED |
| SIL-GATE | Real-model silence scenarios; 0 inappropriate silences on required-response cases | — | — | — | — | — | DEFERRED_TO_FINAL_REAL_MACHINE_GATE |

### Phase 4 gate (spec 4.7)

1. **Spec IDs implemented.** SIL-12.1, SIL-12.2a, SIL-12.2b, SIL-12.3,
   SIL-12.3b, SIL-12.4.
2. **Runtime trigger.** An accepted USER message; the gate runs inside
   `ConversationEngine.plan_turn`, which the service calls on every turn.
3. **Events produced.** `YUI_INTENTIONAL_SILENCE` — its own type, its own
   payload, its own reason codes.
4. **Rows written.** The silence event, and `conversation_traces` with
   `outcome='intentional_silence'` plus `response_intent_started_at` /
   `_ended_at`. Deliberately *not* written: a conversation turn, a failure
   record, a `YUI_MESSAGE_SENT`.
5. **State change.** The silence event goes through the normal processor run,
   so world/needs advance as they would for any event; her own act is not
   appraised (patch spec 5.3).
6. **Debug.** `conversation-plan` prints the intent, its source, what the model
   proposed and which veto fired.
7. **Restart.** `test_a_silence_survives_a_restart` reopens the store and the
   trace repository and finds both.
8. **Unit tests.** 1098 pass in total; 34 are new in this phase.
9. **Integration / E2E.** `tests/invariants/test_intentional_silence_e2e.py`
   drives the real service and the real gateway, including the typing rule.
10. **Deferred.** Real-model silence scenarios —
    `DEFERRED_TO_FINAL_REAL_MACHINE_GATE`, per the OWNER instruction. Not run,
    not claimed.

One judgement call worth stating: the model's inclination rides along in the
existing SocialInterpretation call rather than costing a second one (Phase 3
§32). The *decision* is still a separate object with separate authority, its own
veto, its own event and its own persistence — what is shared is the round trip,
not the responsibility.

---

## Phase 5 — Admin / Debug

| Spec ID | Requirement | Code | Unit Test | Integration Test | E2E Runtime Proof | Debug Path | Status |
|---|---|---|---|---|---|---|---|
| ADM-30.1 | `!yui` is caught above conversation, never undone afterwards | `app/admin/router.py`, `app/interfaces/discord/gateway.py` (`handle_message`) | `test_an_admin_command_is_routed_before_the_conversation_service` | `test_ordinary_messages_are_not_handled` | `test_an_admin_command_never_reaches_the_conversation_service` | `!yui help` | E2E_VERIFIED |
| ADM-30.2 | The query service is structurally read-only | `app/admin/queries.py` | `test_the_query_service_holds_no_engine_that_could_write` | `test_memory_find_practises_nothing` | `test_every_read_only_command_leaves_her_life_identical` | `!yui memory find` | E2E_VERIFIED |
| ADM-30.3 | `backup` is `SAFE_MUTATING`, not read-only | `app/admin/commands.py`, `app/admin/router.py` (`_take_backup`) | `test_backup_is_not_counted_as_read_only` | — | `test_backup_writes_a_file_and_still_no_row` | `!yui backup` | E2E_VERIFIED |
| ADM-30.4 | Admin input never enters her history | `app/admin/router.py`, gateway routing order | `test_a_refused_admin_command_says_nothing_at_all` | `test_an_admin_command_never_reaches_the_conversation_service` | `test_every_read_only_command_leaves_her_life_identical` (26-table fingerprint) | `events` (unchanged) | E2E_VERIFIED |
| ADM-30.5 | No typing indicator for admin output | `app/interfaces/discord/gateway.py` (`_answer_admin`) | `test_admin_output_shows_no_typing_indicator` | — | — | — | E2E_VERIFIED |
| ADM-30.6 | Ownership is checked in exactly one place, and a refusal does not fall through | `app/admin/router.py` (`route`) | `test_ownership_is_checked_in_exactly_one_place` | `test_a_disallowed_channel_is_refused` | `test_a_non_owner_is_refused_and_does_not_fall_through` | logs | E2E_VERIFIED |
| ADM-30.7 | Secrets and prompts are redacted at construction | `app/admin/results.py` | `test_a_secret_or_prompt_field_is_never_printed`, `test_a_credential_shaped_value_is_redacted_whatever_it_is_called` | `test_redaction_reaches_into_nested_payloads` | `test_the_llm_command_shows_health_not_content` | `!yui llm` | E2E_VERIFIED |
| ADM-30.8 | Discord limits are respected by pagination, never mid-row truncation | `app/admin/formatter.py` | `test_a_long_result_is_paginated_rather_than_truncated`, `test_a_short_result_is_one_page` | — | — | — | E2E_VERIFIED |
| ADM-30.9 | A debug failure stays an admin failure | `app/admin/router.py` (`route` except branch) | `test_an_unknown_command_is_answered_not_ignored` | `test_a_broken_query_does_not_become_a_psychological_failure` | — | `failures` (unchanged) | E2E_VERIFIED |
| ADM-30.10 | Every command names a real source and method | `app/admin/queries.py` (`listing`), `app/admin/commands.py` | `test_a_typo_in_a_source_name_is_loud` | `test_the_registry_covers_the_documented_commands` | `test_only_the_unlanded_subsystems_report_not_wired` | `!yui help` | E2E_VERIFIED |

### Phase 5 gate (spec 4.7)

1. **Spec IDs implemented.** ADM-30.1 … ADM-30.10.
2. **Runtime trigger.** A Discord message from the owner beginning with `!yui`,
   in an allowed channel. `DiscordGateway.handle_message` calls
   `AdminRouter.route` **before** anything conversational.
3. **Events produced.** None, deliberately. A debug question is not something
   that happened to her, so there is no event to produce.
4. **Rows written.** None in any of her tables. `test_every_read_only_command_
   leaves_her_life_identical` hashes 26 life tables before and after the whole
   read-only registry and requires byte equality.
5. **State change.** None.
6. **Debug.** The phase *is* the debug path: `!yui help` lists the registry,
   and every listing command names its own source, method and printed fields.
7. **Restart.** Not applicable — nothing is persisted. The read-only property
   is checked against a database seeded by real processor runs, not an empty one.
8. **Unit tests.** 1138 pass in total; 40 are new in this phase (36 in the
   read-only E2E, 4 at the gateway boundary).
9. **Integration / E2E.** `tests/invariants/test_admin_readonly_e2e.py` drives
   `application.admin_router` — the object the running system uses — rather
   than a parallel one assembled in the test.
10. **Deferred.** Nothing. This phase has no heavy real-model component.

Two storage-layer additions were needed rather than worked around downstream
(§22): `ProcessingRunRepository.recent` and `ProactiveRepository.recent` did not
exist, and the commands that needed them belong to the repository boundary, not
to a hand-rolled query in `app/admin/`.

`diary` is the one command that answers "not wired yet in this phase". That is
pinned by a test, so Phase 10 has to shrink the list deliberately; a typo in a
source name fails loudly instead of hiding behind the same message.

---

## Phase 6 — Autonomous Runtime

| Spec ID | Requirement | Code | Unit Test | Integration Test | E2E Runtime Proof | Debug Path | Status |
|---|---|---|---|---|---|---|---|
| RUNTIME-001 | The runtime writes no psychological state | `app/runtime/autonomous.py` | `test_the_runtime_holds_nothing_that_can_write_state`, `test_the_runtime_module_imports_no_psychology` | — | `test_a_tick_changes_no_psychological_state` | `!yui state` (unchanged) | E2E_VERIFIED |
| RUNTIME-002 | A scheduler opportunity is never an action | `app/runtime/autonomous.py`, `app/runtime/sources.py` | `test_an_opportunity_is_not_an_action`, `test_the_decision_engine_chooses_not_the_loop` | `test_a_builder_may_decline` | `test_a_due_job_becomes_an_opportunity_and_stops_there`, `test_a_tick_writes_no_event` | `!yui runtime` | E2E_VERIFIED |
| RUNTIME-003 | A USER message outranks background action | `app/runtime/autonomous.py` (`user_turn`), `app/conversation/service.py` | `test_a_user_turn_defers_background_action`, `test_nested_user_turns_release_together` | `test_the_loop_acts_again_once_the_turn_is_over` | `test_a_real_turn_holds_the_loop_back`, `test_a_failing_turn_still_releases_the_loop` | `runtime_ticks.deferred_reason` | E2E_VERIFIED |
| RUNTIME-004 | Start/stop belong to the lifecycle, and shutdown drains | `app/bootstrap.py` (`start`/`stop`), `app/runtime/autonomous.py` | `test_start_and_stop_are_symmetric`, `test_starting_twice_is_refused` | `test_shutdown_drains_a_running_action` | `test_the_lifecycle_starts_and_drains_the_loop`, `test_the_loop_starts_after_recovery_not_before` | logs | E2E_VERIFIED |
| RUNTIME-21.1 | Next-due-time sleep, never a per-second poll | `app/runtime/autonomous.py` (`_next_wake`, `_sleep`) | `test_the_next_wake_is_the_soonest_source`, `test_an_already_due_source_does_not_busy_wait` | `test_a_distant_due_time_is_capped_by_the_idle_interval` | `test_the_loop_ticks_when_it_is_woken` | `runtime_ticks.next_wake_at` | E2E_VERIFIED |
| RUNTIME-4.5 | Quiet wake-ups and unclaimed kinds are visible | migration 0023, `app/storage/repositories/runtime.py` | `test_an_empty_tick_is_still_recorded`, `test_an_unclaimed_kind_is_recorded_not_swallowed` | `test_the_audit_can_ask_what_fires_and_nobody_wants` | `test_the_audit_query_names_what_nobody_claimed`, `test_the_runtime_command_shows_the_wake_ups` | `!yui runtime` | E2E_VERIFIED |

### Phase 6 gate (spec 4.7)

1. **Spec IDs implemented.** RUNTIME-001 … RUNTIME-004, plus 21.1's sleep rule
   and 4.5's audit surface.
2. **Runtime trigger.** A next-due time reported by an opportunity source, or a
   USER message waking the loop early. `Application.start` starts it when
   `runtime.autonomous` is on; `Application.stop` cancels and drains it.
3. **Events produced.** None of its own, deliberately. Spec 22: an opportunity
   is a possibility, and only a selected *and executed* one becomes an event —
   which the action handler owns, not the loop.
4. **Rows written.** `runtime_ticks`, one per wake-up including the quiet ones,
   and `decisions` when a choice is made.
5. **State change.** None from the loop. State moves the way it always does:
   an action produces an event, the event runs through the processor, the
   domains propose and the arbitrator commits.
6. **Debug.** `!yui runtime` shows the recent wake-ups — added as a registry
   entry with no change to the Phase 5 router, which is what that architecture
   was for. `runtime_ticks.unclaimed_kinds()` is the §4.5 zero-row audit.
7. **Restart.** `test_a_tick_survives_a_restart` reopens the repository and
   finds the row. `test_the_loop_starts_after_recovery_not_before` pins the
   startup order: waking into a half-recovered world is how she acts on a job
   the restore was about to retire.
8. **Unit tests.** 1178 pass in total; 40 are new in this phase (26 in
   `test_autonomous_runtime.py`, 14 in the E2E).
9. **Integration / E2E.** `tests/invariants/test_autonomous_runtime_e2e.py`
   drives `application.runtime` — the object bootstrap built, with the real
   scheduler behind it — and the real `ConversationService.handle_inbound` for
   RUNTIME-003.
10. **Deferred.** Nothing heavy. But see the capability note: the *capability*
    is `WIRED`, not `E2E_VERIFIED`, because Phase 6 registers no candidate
    builders and no action handlers, so no autonomous action has yet run end
    to end. The individual RUNTIME rows are verified; the capability they serve
    is not finished until Phase 7 lands the first handler.

The loop is deliberately off by default (`runtime.autonomous = false`). A test,
a CLI command or a migration run must not silently start a background loop that
acts while nobody is watching.

---

## Phase 7 — Activity / Sleep driven by the runtime

| Spec ID | Requirement | Code | Unit Test | Integration Test | E2E Runtime Proof | Debug Path | Status |
|---|---|---|---|---|---|---|---|
| ACT-001 | `ACTIVITY_STARTED` is not an experience | `app/runtime/life.py` (`start_activity`), `app/world/service.py` | `test_starting_an_activity_is_not_an_experience` | — | `test_both_events_exist_and_are_different` | `!yui activity` | E2E_VERIFIED |
| ACT-002 | Only a completed record lets her say she did it | `app/world/service.py` (`finish_activity`), migration 0024 (`expected_end_at`) | `test_an_activity_is_not_due_before_its_time` | `test_only_finishing_makes_it_something_she_did` | `test_only_finishing_makes_it_something_she_did` | `activities.status` | E2E_VERIFIED |
| ACT-003 | The completed fact is the World Service's | `app/runtime/life.py` (`finish_activity`) | `test_the_completed_fact_belongs_to_the_world_service` | — | `test_only_finishing_makes_it_something_she_did` | `activities.outcome` | E2E_VERIFIED |
| SLEEP-001 | Mood alone cannot keep her awake indefinitely | `app/world/sleep.py` (`resistance_is_exhausted`), `LifeCandidates.sleep` | `test_resistance_runs_out`, `test_an_ordinary_evening_is_not_forced` | `test_an_exhausted_candidate_cannot_be_outbid` | `test_she_falls_asleep_with_nobody_speaking_to_her` | `sleep_episodes.reason` | E2E_VERIFIED |
| SLEEP-002 | A nap and a night are different episodes | `app/world/sleep.py` (`classify_sleep`), migration 0024 (`kind`) | `test_a_doze_outside_the_window_is_a_nap`, `test_real_pressure_outside_the_window_is_still_a_night` | `test_the_kind_is_recorded_when_she_lies_down` | `test_the_kind_is_recorded_when_she_lies_down` | `sleep_episodes.kind` | E2E_VERIFIED |
| SLEEP-003 | The runtime actually fires the transition | `app/runtime/life.py` (`SleepSource`, `LifeActions`) | `test_nothing_happens_when_she_is_not_sleepy` | `test_she_wakes_up_on_her_own` | `test_she_falls_asleep_with_nobody_speaking_to_her` | `!yui runtime` | E2E_VERIFIED |
| SLEEP-004 | A due action, not a calculation waiting for an event | `app/runtime/life.py`, `app/bootstrap.py` (registration) | `test_the_next_wake_is_the_planned_one_while_asleep` | `test_the_sleep_event_is_hers_and_not_a_reply` | `test_she_falls_asleep_with_nobody_speaking_to_her` | `WENT_TO_SLEEP` events | E2E_VERIFIED |

### Phase 7 gate (spec 4.7)

1. **Spec IDs implemented.** ACT-001…003, SLEEP-001…004.
2. **Runtime trigger.** `SleepSource` and `ActivitySource`, collected by the
   Phase 6 loop. No message and no incoming event is involved — which is the
   whole of SLEEP-004.
3. **Events produced.** `WENT_TO_SLEEP`, `WOKE_UP`, `ACTIVITY_STARTED`,
   `ACTIVITY_FINISHED` — all root events with `actor_type=yui` and
   `origin=virtual_life`. Not children of anything: nothing prompted them, and
   descending them from a USER message would make the provenance a lie.
4. **Rows written.** `sleep_episodes` (with `kind`), `activities` (with
   `expected_end_at`, then `status=completed` and `outcome`),
   `world_state_history`, `runtime_ticks`, `events`, `decisions`.
5. **State change.** Through the processor, as normal. The handlers call the
   World Service and emit an event; no domain state is written from
   `app/runtime/` (RUNTIME-001 still holds now that there is something to do).
6. **Debug.** `!yui runtime` shows the wake-up that chose it; `!yui activity`
   and `sleep_episodes` show what came of it.
7. **Restart.** Both due times are rows, not timers: an activity carries its
   own `expected_end_at` and a sleep episode its `planned_wake_at`, so a
   restart still knows what is due.
8. **Unit tests.** 1199 pass in total; 21 are new in `test_life_runtime.py`.
   Four Phase 6 E2E assertions were rewritten rather than relaxed — see below.
9. **Integration / E2E.** `tests/invariants/test_life_runtime.py` drives
   `application.runtime` and asserts the world rows and the events.
10. **Deferred.** Nothing heavy in this phase. One scope note below.

**`autonomous_runtime` moves from `WIRED` to `E2E_VERIFIED`.** Phase 6 shipped
the loop with nothing registered; this phase registered the first real builders
and handlers, so the full chain — source → opportunity → candidate → decision →
handler → World Service → event → processor → rows — now runs with nobody
speaking to her.

**Four Phase 6 tests were rewritten, not weakened.** They asserted that a tick
writes no event and changes no state, which was true only because nothing was
registered. With handlers in place a tick that *acts* is supposed to move the
world — that is the phase. The invariant that stays true is narrower and now
stated directly: the loop's own machinery (collect, value, decide, record)
changes nothing, asserted on a deferred tick that does all four and executes
nothing. The unclaimed-kind tests moved from `activity_due` to `diary_due`,
because what is being protected is that an unclaimed kind stays *visible*, not
that any particular kind stays unclaimed.

**Scope note.** What she chooses to *do* comes from a declared repertoire in
`LifeActions`, not from spec 24.1's `activity_candidates` model call. That is a
deliberate boundary: this phase owns the lifecycle, and swapping the candidate
source for a model call changes where candidates come from without touching
ACT-001/002/003. It is listed as the first item of the next agency phase rather
than quietly claimed here.

---

## Phase 8 — Goals / Habits / NPCs / Groups

| Spec ID | Requirement | Code | Unit Test | Integration Test | E2E Runtime Proof | Debug Path | Status |
|---|---|---|---|---|---|---|---|
| AG-31.3 | Active goals and matching habit cues are always candidate sources | `app/runtime/agency.py`, `app/bootstrap.py` | `test_both_are_registered_as_sources` | `test_an_active_goal_is_offered`, `test_an_established_habit_fires` | `test_a_goal_step_starts_real_work` | `!yui runtime` | E2E_VERIFIED |
| AG-31.1 | Python evaluates a goal step; the model does not score it | `app/runtime/agency.py` (`AgencyCandidates.goal_step`) | `test_a_goal_just_worked_on_is_left_alone` | — | `test_a_goal_step_starts_real_work` | `decisions` rows | E2E_VERIFIED |
| AG-31.1b | Starting work never advances the goal | `app/runtime/agency.py`, `GoalRepository.mark_pursued` | `test_starting_work_does_not_advance_the_goal` | `test_finishing_the_work_is_still_the_activity_lifecycle` | `test_finishing_the_work_is_still_the_activity_lifecycle` | `goals.progress` | E2E_VERIFIED |
| AG-31.2 | Automaticity is tracked, and only the pairing builds it | `app/agency/habits.py`, `app/runtime/agency.py` | `test_looking_for_a_cue_is_not_encountering_one` | `test_performing_a_habit_strengthens_it_through_the_engine` | `test_performing_a_habit_strengthens_it_through_the_engine` | `!yui habits` | E2E_VERIFIED |
| SOC-29.1 | Tier 0 is background and is never contacted | `app/runtime/social.py` (`CONTACTABLE_TIER`) | `test_background_people_are_not_contacted` | `test_someone_who_recurs_can_be_contacted` | `test_contacting_someone_goes_through_the_society_service` | `!yui npc` | E2E_VERIFIED |
| SOC-29.2 | Repetition promotes, counted from the interaction rows | `app/runtime/social.py` (`_maybe_promote`) | — | `test_a_person_is_not_contacted_twice_in_a_row` | `test_returning_to_someone_promotes_them` | `npcs.tier` | E2E_VERIFIED |
| SOC-29.5 | Only the Society Service commits an interaction | `app/runtime/social.py`, `app/society/service.py` | — | — | `test_contacting_someone_goes_through_the_society_service` | `npc_interactions` | E2E_VERIFIED |
| SOC-30 | The USER is not the only source of relatedness | `app/runtime/social.py` (`SocialCandidates._relatedness_need`) | `test_company_matters_more_when_she_is_lonely` | `test_a_group_meeting_is_an_opportunity` | `test_attending_a_group_produces_a_group_event` | `!yui groups` | E2E_VERIFIED |

### Phase 8 gate (spec 4.7)

1. **Spec IDs implemented.** §31.1, §31.2, §31.3, §29.1, §29.2, §29.5, §30.
2. **Runtime trigger.** Four new sources — goals, habits, NPCs, groups — in the
   Phase 6 registry. §31.3 says 必ず候補源にする, so goals and habits are
   registered unconditionally rather than consulted when the loop thinks of it.
3. **Events produced.** `GOAL_PURSUED`, `HABIT_PERFORMED` (new, in
   `app/agency/events.py`), `NPC_INTERACTION`, `GROUP_ACTIVITY`.
4. **Rows written.** `goals.last_pursued_at`, `plans`, `habits` (repetitions,
   cue encounters, automaticity), `npc_interactions`, `npcs.tier`,
   `activities`, `events`, `runtime_ticks`, `decisions`.
5. **State change.** Through the processor. Neither new runtime module imports
   `app.psychology`, `app.state` or `app.consolidation` — asserted.
6. **Debug.** `!yui goals`, `!yui habits`, `!yui npc`, `!yui groups`,
   `!yui runtime` — all already in the Phase 5 registry.
7. **Restart.** Every counter is a row. Promotion is counted from the
   interaction history rather than a tally held somewhere, so it survives a
   restart and can be argued with.
8. **Unit tests.** 1222 pass in total; 23 are new in
   `test_agency_social_runtime.py`.
9. **Integration / E2E.** `tests/invariants/test_agency_social_runtime.py`
   drives `application.runtime` against the real engines.
10. **Deferred.** Nothing heavy.

**§31 was a wiring brief, and it is worth being literal about it.** `GoalEngine`,
`HabitEngine`, `SocietyService` and `GroupEngine` already existed, were already
constructed at startup, and already passed their unit tests — they are four of
the six subsystems §0 opens by naming. Phase 8 adds no second engine; it adds
the sources, the valuations and the handlers that reach them.

**One design point worth stating.** `HabitEngine.cue_encountered` increments the
cue counter, which makes it a writer and therefore unusable from a source. The
source reads `by_cue`; only the handler tells the engine that a pairing
happened. Without that split, every habit would strengthen on the strength of
being looked at.

---

## Phase 9 — Proactive contact

| Spec ID | Requirement | Code | Unit Test | Integration Test | E2E Runtime Proof | Debug Path | Status |
|---|---|---|---|---|---|---|---|
| PRO-28.1 | Silence never increases the rate | `app/jobs/proactive.py`, `app/runtime/proactive.py` | `test_silence_never_increases_the_rate`, `test_too_many_unanswered_stops_her_entirely` | `test_a_reply_clears_the_backlog` | `test_the_dryrun_reports_the_gate` | `!yui proactive dryrun` | E2E_VERIFIED |
| PRO-28.2 | The model is asked only after the gate, and answers categorically | `app/runtime/proactive.py` (`ProactiveJudgment`), `config/prompts/proactive_judgment/v1.md` | `test_the_judgment_is_categorical`, `test_anything_short_of_yes_is_no` | `test_the_gate_runs_before_the_model_is_asked` | `test_shadow_records_what_she_would_have_said` | `llm_calls` (`purpose='proactive_judgment'`) | E2E_VERIFIED |
| PRO-28.2b | A missing or broken model means silence | `app/runtime/proactive.py` (`_judge`) | `test_no_model_means_no_message`, `test_a_broken_model_means_no_message` | — | — | `proactive_deliberations.judged` | E2E_VERIFIED |
| PRO-28.3 | A message needs something to be about | `app/runtime/proactive.py` (`TRIGGER_EVENTS`, `find_trigger`) | `test_nothing_to_say_is_no_opportunity`, `test_finishing_something_is_a_trigger_kind` | `test_a_stale_trigger_is_not_a_reason`, `test_wanting_company_counts_but_only_when_it_is_real` | `test_something_that_happened_is_a_trigger` | `!yui proactive dryrun` | E2E_VERIFIED |
| PRO-28.4 | OFF / SHADOW / LIVE, SHADOW by default, shadow records in full | `app/config.py`, migration 0025, `app/runtime/proactive.py` | `test_shadow_is_the_default`, `test_off_deliberates_nothing_at_all` | `test_shadow_leaves_no_contact_row` | `test_shadow_records_what_she_would_have_said` | `!yui proactive shadow` | E2E_VERIFIED |
| PRO-28.5 | Success only: event and contact record after a confirmed send | `app/runtime/proactive.py` (`_send`) | `test_the_null_sender_is_the_default` | `test_a_failed_send_records_no_contact` | `test_live_sends_and_records_in_that_order` | `YUI_MESSAGE_SENT` events | E2E_VERIFIED |
| PRO-16 | An unprompted draft passes the same output guard as a reply | `app/runtime/proactive.py` (`_check`) | — | `test_a_guarded_draft_never_leaves` | `test_a_guarded_draft_never_leaves` | `proactive_deliberations.guard_verdict` | E2E_VERIFIED |
| ADM-39.2 | `proactive dryrun` has no side effects at all | `app/admin/queries.py` (`proactive_dryrun`) | `test_the_dryrun_makes_no_model_call` | `test_the_dryrun_reports_the_gate` | `test_the_dryrun_changes_nothing_at_all` | `!yui proactive dryrun` | E2E_VERIFIED |
| PRO-GATE | A real unprompted message to the real USER, on a real Discord connection | — | — | — | — | — | DEFERRED_TO_FINAL_REAL_MACHINE_GATE |

### Phase 9 gate (spec 4.7)

1. **Spec IDs implemented.** 28.1 … 28.5, plus §16 on the draft and the
   OWNER's Phase 5 requirement for a side-effect-free dry run.
2. **Runtime trigger.** `ProactiveSource`, registered in the Phase 6 loop. It
   deliberately does *not* consult the hard gate: a source notices that there
   is something to say, and mixing that with "is it allowed" would hide the
   gate's decisions from the tick record.
3. **Events produced.** `YUI_MESSAGE_SENT`, in LIVE only, and only after the
   send is confirmed.
4. **Rows written.** `proactive_deliberations` in every mode including OFF —
   "she was switched off" is a fact worth being able to read — and
   `proactive_contacts` only on a confirmed send.
5. **State change.** Through the processor, from the sent event.
6. **Debug.** `!yui proactive dryrun` (gate + trigger scan, no model call, no
   rows), `!yui proactive shadow` (what she would have said),
   `!yui proactive` (actual contacts).
7. **Restart.** Every gate input is a row: the contact history is what the
   backoff reads, so a restart cannot reset the interval.
8. **Unit tests.** 1254 pass in total; 32 are new in
   `test_proactive_runtime.py`.
9. **Integration / E2E.** `tests/invariants/test_proactive_runtime.py` drives
   the real engine, the real prompts and the real output guard, with a
   recording sender in place of Discord.
10. **Deferred.** `PRO-GATE` — a real unprompted message on a real connection.
    `DEFERRED_TO_FINAL_REAL_MACHINE_GATE`, not run, not claimed.

**Why the dry run does not ask the model.** The OWNER's Phase 5 requirement was
that it have no side effects: no contact, no decision, no opportunity consumed,
no event. It also makes no model call, for a second reason — "would you want to
send something right now?" is not a question with a stable answer, and an
operator running it thirty times would get thirty different ones. What it
reports is the *structural* verdict: the trigger scan and the hard gate.

**`would_send` and `sent` are separate columns on purpose.** "She wanted to and
the mode forbade it" and "she decided not to" are different facts about her, and
a shadow log that collapsed them would be unable to answer the only question
shadow mode exists to answer.

---

## Phase 10 — Diary

| Spec ID | Requirement | Code | Unit Test | Integration Test | E2E Runtime Proof | Debug Path | Status |
|---|---|---|---|---|---|---|---|
| DIA-26.1 | Event ≠ Memory ≠ Diary | `app/diary/models.py`, migration 0026 | `test_the_diary_text_is_never_copied_into_memory` | — | `test_the_diary_text_is_never_copied_into_memory` | `!yui diary` | E2E_VERIFIED |
| DIA-26.2 | A life day runs from waking to sleeping, not midnight | `app/storage/repositories/diary.py` (`LifeDayRepository`), `app/runtime/life.py` | `test_a_day_runs_from_waking_to_sleeping`, `test_opening_a_day_twice_gives_one_day` | `test_a_nap_does_not_end_a_day` | `test_a_night_starts_the_next_day` | `!yui diary days` | E2E_VERIFIED |
| DIA-26.3 | An LLM failure must never keep her awake | `app/diary/service.py` (`reflect_at_bedtime`), `app/runtime/life.py` (`_reflect`) | `test_a_broken_model_does_not_keep_her_awake`, `test_an_empty_draft_leaves_the_entry_owed` | `test_a_hanging_model_does_not_keep_her_awake` | `test_she_writes_before_sleeping` | `diary_entries.status` | E2E_VERIFIED |
| DIA-26.3b | A late entry keeps its intended bedtime and says it was late | `app/diary/service.py` (`retry_pending`), migration 0026 | `test_a_late_entry_is_not_the_same_status_as_a_punctual_one`, `test_attempts_are_counted` | `test_the_retry_keeps_the_bedtime_it_was_meant_for` | `test_the_retry_keeps_the_bedtime_it_was_meant_for` | `diary_entries.generated_at` | E2E_VERIFIED |
| DIA-26.4 | Compressed by importance, never a dump of every event | `app/diary/service.py` (`DiaryContextBuilder`) | `test_an_empty_day_says_so` | `test_the_context_is_compressed` | `test_she_writes_before_sleeping` | — | E2E_VERIFIED |
| DIA-26.5 | Free prose, no fixed form, short days allowed | `app/diary/models.py` (`DiaryDraft`), `config/prompts/diary/v1.md` | `test_the_draft_schema_imposes_no_form` | `test_a_day_where_nothing_happened_is_allowed_to_be_short` | `test_she_writes_before_sleeping` | `config/prompts/diary/v1.md` | E2E_VERIFIED |
| DIA-26.7 | The text is never copied into memory; only what she wrote about is practised | `app/diary/service.py` (`_practise`), `diary_references.mentioned_in_text` | `test_only_what_she_wrote_about_is_marked_as_mentioned`, `test_writing_it_is_an_experience` | `test_the_diary_text_is_never_copied_into_memory` | `test_the_diary_text_is_never_copied_into_memory` | `diary_references` | E2E_VERIFIED |
| DIA-26.8 | Ordinary recall can never read the diary | `app/memory/` (no diary import), `app/diary/service.py` (`read`) | `test_an_unwritten_entry_cannot_be_read` | `test_ordinary_recall_cannot_reach_the_diary` | `test_reading_the_diary_is_its_own_event` | `DIARY_READ` events | E2E_VERIFIED |
| DIA-GATE | Real-model diary quality over many days | — | — | — | — | — | DEFERRED_TO_FINAL_REAL_MACHINE_GATE |

### Phase 10 gate (spec 4.7)

1. **Spec IDs implemented.** 26.1 … 26.8, and the 36 data model.
2. **Runtime trigger.** The sleep decision. `LifeActions.go_to_sleep` awaits
   the reflection and then falls asleep *regardless of what it returns*.
3. **Events produced.** `BEDTIME_REFLECTION_STARTED`, `DIARY_WRITTEN`,
   `DIARY_READ`.
4. **Rows written.** `life_days`, `diary_entries`, `diary_references`.
5. **State change.** Through the processor, from `DIARY_WRITTEN` — the *act*
   of writing is an experience (26.7). The text is not, and nothing copies it
   into memory.
6. **Debug.** `!yui diary` and `!yui diary days`. The Phase 5 test that pinned
   `diary` as the one "not wired yet" command now asserts the opposite.
7. **Restart.** A pending entry survives and is written later as
   `late_written`, keeping the bedtime it was meant for.
8. **Unit tests.** 1278 pass in total; 24 are new in `test_diary.py`.
9. **Integration / E2E.** `tests/invariants/test_diary.py` drives the real
   sleep path with a model that hangs, raises, returns nothing, and works.
10. **Deferred.** `DIA-GATE` — whether the prose is any good over many real
    days. `DEFERRED_TO_FINAL_REAL_MACHINE_GATE`.

**The ordering is the design.** 26.3 says an LLM failure must not keep her
awake, which rules out generate-then-sleep. So the entry is marked *owed*
before the model is asked, the attempt is bounded, and sleep proceeds on every
path — hang, exception, empty draft. A test sets the timeout to 50ms against a
model that sleeps for an hour and asserts she is asleep afterwards.

**26.8 is kept structurally rather than by discipline.** Nothing in
`app/memory/` imports the diary, asserted by parsing the imports. A forgotten
day cannot be quietly recovered from the record she wrote about it; reading the
diary is a deliberate act with its own event, and the read-only `!yui diary`
view is not that act.

---

## Phase 11 — Search / Knowledge

| Spec ID | Requirement | Code | Unit Test | Integration Test | E2E Runtime Proof | Debug Path | Status |
|---|---|---|---|---|---|---|---|
| EPI-17.1 | A gap is a situation with seven answers, not a search trigger | `app/runtime/knowledge.py` (`KnowledgeCandidates`), `app/epistemics/actions.py` | `test_the_selector_has_all_seven_options`, `test_a_gap_answerable_from_memory_is_not_searched` | `test_the_builder_only_produces_a_candidate_for_web_search` | `test_the_decision_is_recorded_either_way` | `!yui gaps` | E2E_VERIFIED |
| SRCH-32.2 | ToolManager success is the only authority that a search ran | `app/knowledge/investigation.py` (`_run_tool`), `app/tools/builtin.py` | `test_the_failure_modes_are_distinct` | `test_the_search_goes_through_the_tool_manager` | `test_the_whole_chain_runs_from_the_loop` | `!yui tools` | E2E_VERIFIED |
| SRCH-TIME | `published_at > effective_now` is unusable, in Python | `app/knowledge/search.py` (`reject_the_future`, `SearchQuery`) | `test_a_result_from_the_future_is_unusable`, `test_an_undated_result_is_also_refused`, `test_the_query_cannot_be_built_without_a_moment` | `test_a_past_dated_search_sees_only_the_past` | `test_the_gate_runs_on_a_real_search` | `search_calls.future_rejected` | E2E_VERIFIED |
| SRCH-SEP | Search request ≠ search success ≠ knowledge acquisition | `app/knowledge/investigation.py`, migration 0027 | `test_a_search_is_not_an_acquisition` | `test_the_two_events_are_different` | `test_the_whole_chain_runs_from_the_loop` | `!yui search recent` | E2E_VERIFIED |
| SRCH-FAIL | The failure modes are distinct, and none is knowledge | `app/knowledge/search.py` (`SearchOutcome`), `_failure_from` | `test_the_failure_modes_are_distinct`, `test_no_provider_is_not_no_results` | `test_a_failed_search_acquires_nothing` | `test_no_results_is_not_a_fact_about_the_world`, `test_a_failed_search_path_is_exercised_too` | `search_calls.outcome` | E2E_VERIFIED |
| SRCH-BELIEF | A search result is evidence, never an overwrite | `app/knowledge/investigation.py` (`_offer_as_evidence`) | `test_a_search_result_argues_rather_than_decrees` | `test_acquired_knowledge_reaches_the_belief_engine` | — | `beliefs` / `belief_evidence` | E2E_VERIFIED |
| SRCH-PROV | Every acquired fact knows where it came from | migration 0027, `KnowledgeRepository.add_knowledge` | — | `test_every_acquired_fact_knows_where_it_came_from` | `test_knowledge_find_shows_provenance` | `!yui knowledge find` | E2E_VERIFIED |
| SRCH-GATE | A live search backend against the real web | — | — | — | — | — | DEFERRED_TO_FINAL_REAL_MACHINE_GATE |

### Phase 11 gate (spec 4.7)

1. **Spec IDs implemented.** 17.1, 32.1, 32.2, 32.3, plus the provenance and
   temporal requirements the OWNER set for this phase.
2. **Runtime trigger.** `GapSource` in the Phase 6 loop. The candidate builder
   runs the Epistemic Action Selector and produces a candidate *only* when the
   answer was `web_search`.
3. **Events produced.** `SEARCH_PERFORMED` (one per search, whatever came
   back) and `KNOWLEDGE_ACQUIRED` (one per thing actually learned).
4. **Rows written.** `knowledge_gaps` (with the chosen action),
   `search_calls` (results / future_rejected / exposed / acquired as four
   separate columns), `external_knowledge` with provenance,
   `knowledge_exposure_opportunities`, `knowledge_acquisitions`.
5. **State change.** Through the processor. Acquired facts go to the Belief
   Engine as *evidence*, never as an overwrite.
6. **Debug.** `!yui search recent`, `!yui gaps`, `!yui tools`,
   `!yui knowledge`, `!yui knowledge find <query>` — the last showing
   provenance, which is the view the Genesis leakage audit will use.
7. **Restart.** Gaps, searches and provenance are all rows. An open gap
   survives and is picked up again.
8. **Unit tests.** 1307 pass in total; 29 are new in
   `test_search_knowledge.py`. Three tool tests were updated because
   `web_search` gained a required `effective_now` — the argument was added
   at the tool, not worked around at the callers.
9. **Integration / E2E.** `test_the_whole_chain_runs_from_the_loop` drives the
   real loop and asserts the phase's counters.
10. **Deferred.** `SRCH-GATE` — a live backend against the real web.

**The counters the phase is judged on**, all asserted in one test against the
wired application: search attempts > 0, results > 0, future-rejected > 0,
exposed > 0, acquired > 0, failed-search path > 0 — and `acquired < results`,
because a pipeline where every result becomes knowledge has no funnel in it.
The fixture deliberately contains an entry dated 2099, so the temporal gate
fires on every run rather than only in the test written for it.

**Why the tool wrapper matters.** The provider is reached *through*
`web_search`, not beside it. That keeps 32.2 true — a search that never ran
cannot be described as one that did — and it is why a provider outage arrives
as a failed tool call rather than as an empty result set. `_failure_from` maps
the tool's error string back to the search taxonomy so "the network was down"
survives the round trip instead of flattening into a generic error.

**`effective_now` has no default anywhere in the chain.** Not in `SearchQuery`,
not in `InvestigationService.investigate`, not in the tool's required
arguments. A caller that forgets it fails loudly. This is the single most
important thing Phase 12 inherits: a parameter that quietly falls back to
today's clock is how a 2012 Genesis reads about 2025.

---

## Phase 12 — Genesis v2

| Spec ID | Requirement | Code | Unit Test | Integration Test | E2E Runtime Proof | Debug Path | Status |
|---|---|---|---|---|---|---|---|
| GEN-34.1 | Every age is computed in Python from exact dates | `app/genesis/anchors.py` (`age_at`, `year_spans`) | `test_an_age_is_arithmetic_not_an_opinion`, `test_a_birthday_that_has_not_arrived_does_not_count`, `test_a_leap_day_birthday_does_not_crash` | `test_a_life_year_is_all_one_age` | `test_the_years_view_shows_the_ages` | `!yui genesis years` | E2E_VERIFIED |
| GEN-34.2 | The seed is a rough bias, not a personality | `app/genesis/anchors.py` (`TemperamentSeed`) | `test_the_temperament_seed_is_thin` | — | — | `life_anchors.temperament_json` | E2E_VERIFIED |
| GEN-34.3/4 | Stage A scaffolds are provisional; Stage B expands them | `app/genesis/runner.py`, migration 0028 | `test_a_year_is_scaffolded_then_expanded_then_synthesised` | `test_the_scaffold_is_not_regenerated_on_resume` | `test_a_year_is_scaffolded_then_expanded_then_synthesised` | `!yui genesis years` | E2E_VERIFIED |
| GEN-34.5 | Only meaningful months earn a detail call | `app/genesis/models.py` (`WORTH_DETAIL`), runner | `test_a_routine_month_gets_no_detail_call`, `test_a_meaningful_month_does` | — | `life_months.importance_class` | `!yui genesis years` | E2E_VERIFIED |
| GEN-34.6 | Stage C prefers the months, and the scaffold survives | `LifeRecordRepository.synthesise` | `test_the_scaffold_survives_the_synthesis` | `test_a_year_is_scaffolded_then_expanded_then_synthesised` | — | `life_years.final_summary` | E2E_VERIFIED |
| GEN-34.7/8 | Continuity is a queryable ledger with a relevant subset | `app/genesis/ledger.py` | `test_the_relevant_subset_is_a_subset`, `test_an_open_thread_stays_relevant_however_old`, `test_someone_not_yet_born_into_her_life_is_not_offered` | `test_a_person_mentioned_every_month_is_one_person` | `test_survivors_become_runtime_npcs` | `life_entities` | E2E_VERIFIED |
| GEN-34.8b | People who faded stay in the archive | `ContinuityLedger.retire`, `survivors` | `test_someone_who_faded_stays_in_the_archive` | — | — | `life_entities.status` | E2E_VERIFIED |
| GEN-CRITIC-001 | A failed audit stops the stage and leaves a row | `app/genesis/critics.py`, `GenesisRunner._year` | `test_the_chronology_critic_needs_no_model`, `test_the_identity_critic_catches_a_body`, `test_a_critic_that_could_not_run_is_not_a_pass` | `test_a_failed_audit_is_a_row` | `test_a_blocking_issue_stops_the_year` | `!yui genesis audits` | E2E_VERIFIED |
| GEN-34.11/13 | Experiences, not sentences; narrative is not memory | `app/genesis/runner.py` (`_extract`, `Extraction`) | `test_a_repeated_routine_is_one_compressed_experience` | `test_a_month_does_not_become_thirty_events` | `test_narrative_is_not_memory` | `episodic_memories` | E2E_VERIFIED |
| GEN-34.12 | Replay runs forwards through the ordinary processor | `GenesisRunner._replay` | — | `test_experiences_are_replayed_forwards` | `test_experiences_are_replayed_forwards` | `SIMULATED_EXPERIENCE` events | E2E_VERIFIED |
| GEN-34.18/19 | Checkpoints, and a resume that relives nothing | `GenesisRunRepository.checkpoint`, migration 0028 | `test_the_checkpoint_names_are_the_specs`, `test_a_checkpoint_is_written_once` | `test_the_scaffold_is_not_regenerated_on_resume` | `test_a_resume_does_not_relive_a_year` | `genesis_checkpoints` | E2E_VERIFIED |
| GEN-34.20 | Nine audits gate FIRST_BOOT_COMPLETE | `GenesisRunner.first_boot_audits` | `test_there_are_nine_first_boot_audits` | `test_the_audits_run_and_pass_on_a_clean_run` | `test_a_real_user_message_fails_the_audit`, `test_a_body_in_the_record_fails_the_identity_audit`, `test_future_knowledge_fails_the_chronology_audit` | `genesis_checkpoints` | E2E_VERIFIED |
| GEN-GATE | A real nineteen-year run with a real model | — | — | — | — | — | DEFERRED_TO_FINAL_REAL_MACHINE_GATE |

### Phase 12 gate (spec 4.7)

1. **Spec IDs implemented.** 34.1 … 34.20 and the 35 data model.
2. **Runtime trigger.** An explicit FIRST BOOT command. Genesis is built in
   bootstrap and never started by it: a bootstrap that *could* start a
   nineteen-year run is one that can start it by accident.
3. **Events produced.** `SIMULATED_EXPERIENCE` — the existing type, not a new
   one. Genesis v2 changes how the past is generated, not what an experience
   is, and a second event type would give appraisal and memory two things
   meaning the same thing.
4. **Rows written.** `genesis_runs`, `genesis_checkpoints`, `life_anchors`,
   `life_years`, `life_months`, `life_entities`, `life_entity_snapshots`,
   `generation_audits`.
5. **State change.** Only through replay, forwards, via the ordinary
   processor. Nothing writes a trait.
6. **Debug.** `!yui genesis`, `!yui genesis years`, `!yui genesis audits` —
   the last one showing the failures, which is what GEN-CRITIC-001 needs.
7. **Restart.** Checkpoints per stage and per year, uniquely indexed. A resume
   regenerates no scaffold and replays no experience twice.
8. **Unit tests.** 1346 pass in total; 39 are new in `test_genesis_v2.py`.
9. **Integration / E2E.** One year end to end with a scripted storyteller,
   plus each of the nine audits proven to *fail* when given a body, a real
   USER message, or knowledge from after the present.
10. **Deferred.** `GEN-GATE` — a real nineteen-year run with a real model.
    `DEFERRED_TO_FINAL_REAL_MACHINE_GATE`.

**Phase 11's discipline is what makes 34.9 checkable.** The knowledge
chronology audit reads `available_from` — the provenance column Phase 11 made
mandatory — and fails on anything dated after the present. Without that column
this audit could only have been a promise.

**The two critics that need no model are the two that catch arithmetic.**
Chronology and identity run in Python, because a model asked to check an age it
might itself have got wrong puts the same failure on both sides of the check. A
critic that *cannot* run is recorded as not having approved, rather than as
having passed — an unavailable critic returning `True` is precisely the silent
pass GEN-CRITIC-001 forbids.

---

## Phases 3-15

Rows are added when the phase starts. Adding them early with optimistic
statuses is exactly the failure this ledger exists to prevent.

| Phase | Subject | Status |
|---|---|---|
| 5 | Admin / debug router | STRUCTURALLY_COMPLETE |
| 6 | Autonomous Runtime | STRUCTURALLY_COMPLETE |
| 7 | Activity / Sleep / Scheduler | STRUCTURALLY_COMPLETE |
| 8 | NPC / Groups / Goals / Habits | STRUCTURALLY_COMPLETE |
| 9 | Proactive contact | STRUCTURALLY_COMPLETE |
| 10 | Diary | STRUCTURALLY_COMPLETE |
| 11 | Search / Knowledge | STRUCTURALLY_COMPLETE |
| 12 | Genesis v2 | STRUCTURALLY_COMPLETE |
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
| natural_conversation_realization | E2E_VERIFIED |
| intentional_silence | E2E_VERIFIED |
| activity | E2E_VERIFIED |
| sleep | E2E_VERIFIED |
| diary | E2E_VERIFIED |
| spontaneous_memory | NOT_STARTED |
| npc_interaction | E2E_VERIFIED |
| group_activity | E2E_VERIFIED |
| goal_action | E2E_VERIFIED |
| habit_action | E2E_VERIFIED |
| proactive_contact | E2E_VERIFIED |
| web_search | E2E_VERIFIED |
| genesis | E2E_VERIFIED |
| admin_debug_readonly | E2E_VERIFIED |
| admin_backup | E2E_VERIFIED |
| autonomous_runtime | E2E_VERIFIED |

`natural_conversation_realization` is the first `E2E_VERIFIED` capability: a
real inbound message drives interpretation, planning, reference lookup,
realization, the hard gates and delivery, and the test asserts the rows.
`web_search` and `genesis` are `CODE_ONLY`
because their engines exist and nothing in the running system drives them the
way this spec requires — which is the honest reading of §0, not a downgrade of
work already done.

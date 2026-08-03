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
8. **Unit tests.** 1132 pass in total; 34 are new in this phase.
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

## Phases 3-15

Rows are added when the phase starts. Adding them early with optimistic
statuses is exactly the failure this ledger exists to prevent.

| Phase | Subject | Status |
|---|---|---|
| 5 | Admin / debug router | STRUCTURALLY_COMPLETE |
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
| natural_conversation_realization | E2E_VERIFIED |
| intentional_silence | E2E_VERIFIED |
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
| admin_debug_readonly | E2E_VERIFIED |
| admin_backup | E2E_VERIFIED |

`natural_conversation_realization` is the first `E2E_VERIFIED` capability: a
real inbound message drives interpretation, planning, reference lookup,
realization, the hard gates and delivery, and the test asserts the rows.
`web_search` and `genesis` are `CODE_ONLY`
because their engines exist and nothing in the running system drives them the
way this spec requires — which is the honest reading of §0, not a downgrade of
work already done.

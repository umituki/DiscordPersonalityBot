# YUI v2 全不具合修正パッチ仕様書

- 文書種別: Patch Architecture / Implementation Specification
- 対象: `umituki/DiscordPersonalityBot` / YUI v2
- 対象ブランチ基準: `claude/spec-based-development-o4nugw`
- 目的: 2026-08-02 実機検証で判明した Runtime Conversation / State Concurrency / Past Simulation / Memory / Historical Knowledge / FIRST BOOT Audit の不具合を、既存 invariant を維持したまま修正する
- 実装主体: Claude Code
- 本文書の位置づけ: 既存 `docs/YUI_v2_SPEC.md` を置き換えるものではない。今回の不具合に対する修正差分仕様であり、矛盾する場合は OWNER 確認なしに既存 invariant を弱めてはならない
- 実装コード: 本文書には含めない

---

# 0. このパッチの前提

## 0.1 維持する invariant

1. LLM は人格・DB State の Authority ではない。
2. Python / SQLite が Dynamic State の Authority である。
3. Objective Archive と Subjective Memory を分離する。
4. External Knowledge と YUI が実際に取得・保持した Knowledge を分離する。
5. 各 State Domain は Single Writer を持つ。
6. Subsystem は他 Domain を直接変更せず Proposal / Evidence を返す。
7. Dynamic State 更新は Arbitration + Transaction を通す。
8. USER / NPC / simulated past / real Discord / admin を混同しない。
9. 単一 Event から Personality / Values を直接変更しない。
10. FIRST BOOT 前後の履歴境界を壊さない。
11. Past Simulation は最終人格を先に決めて逆算してはならない。
12. Personality / Values は repeated, persistent, cross-context evidence からのみ徐々に変化する。
13. 本番 DB を手編集しない。

## 0.2 実機で確認済みの不具合

### Runtime Conversation

- `appraisal`: 平均約104.8秒、最大113.1秒。
- 1回目のAppraisalが120秒 timeout、その後の再試行が約112〜113秒。
- `dialogue_act`: 平均約4.9秒だが、各turnで3試行し、MODEL RESPONSEが空文字になって `invalid_json`。
- `conversation_reply`: 約3.7〜4.1秒。
- `invalid_json`: 15件。
- `llm_timeout`: 3件。
- Ollama `qwen3.5:9b`: 100% GPU、context 8192。

### Conversation Context

2回目の USER reply context に直前の YUI 発言が欠落した。

### State Concurrency

2回目 USER Event の processing run が `ConcurrentStateWriteError('world.circadian_phase changed since version 3')` で失敗し、State Change が0件になった。

### Past Simulation / Genesis

- 2007-07-04〜2026-08-01。
- 238 blocks / 238 experiences。
- routine 170 / minor 50 / meaningful 14 / major 4。
- narrated 18。
- FIRST BOOT は成功。

しかし、

```text
characteristic_adaptations = 0
deep_update_candidates = 0
personality_history = 0
value_history = 0
external_knowledge = 0
knowledge_acquisitions = 0
simulated episodic memory path = 未接続
```

Genesis consolidation も、

```text
changes_read = 75
adaptations_moved = 0
candidates_raised = 0
deep_updates = 0
semantic_facts = 0
```

で終了した。

---

# 1. 根本原因

1. Ollama request に `think` を明示せず、Thinking対応modelのreasoningが通常structured taskへ混入している。
2. Purposeごとのretry/timeoutが弱く、Appraisal failureがUSER待ち時間へ直結している。
3. ResourceManagerのP0〜P7設計が実 model slot schedulingへ十分接続されていない。
4. Discord送信後のYUI turn projectionより先に高コストpost-send processingが走り、次turnのrecent conversationから直前YUI発言が欠落する。
5. 数分続くrunの間にbackground/world stateが更新され、snapshotがstaleになる。
6. Dialogue Actが空contentで失敗し、実質fallbackだけで返答している。
7. Emotion / Relationship / Personality等のDynamic Stateが最終言語表現へ十分接続されていない。
8. Output Guardが重複、オウム返し、再質問、Dialogue Actとの矛盾を検出しない。
9. `SIMULATED_EXPERIENCE` が `category="internal"` のため通常Appraisalから外れ、Emotion/Growth evidenceが生まれない。
10. PastSimulationEngineからMemoryEngineへの正式なsubjective episodic memory経路がない。
11. Genesisの時間経過をSystemClockで扱う箇所があり、19年のsimulationが数十秒のpersistenceとして扱われ得る。
12. Genesis終了時にDeep Consolidationを1回だけ行うため、repeated/persistent gateを構造的に満たしにくい。
13. Historical Knowledge source/candidateが登録されず0件のまま。
14. FIRST BOOT Auditがpipeline healthを確認せず、空のGrowth/Memory/Knowledgeでも成功する。
15. Simulation Block `event_count` が固定0で監査情報が不正確。

---

# 2. パッチ後の正常経路

## 2.1 Runtime Conversation

```text
Discord inbound
→ admission
→ USER event persist
→ USER turn projection
→ Reply Intent
→ Typing start
→ fresh snapshot
→ fast Appraisal
→ psychology/social proposals
→ arbitration/commit
→ memory recall
→ Dialogue Decision
→ Expression Context
→ final reply generation
→ Conversation Quality Guard
→ Discord send
→ YUI sent event persist
→ YUI turn immediate projection
→ Typing stop
→ post-send background work
```

## 2.2 Past Simulation

```text
Simulated Time
→ Experience
→ Appraisal
→ Emotion / Needs / World / Social State
→ selective Memory Encoding Opportunity
→ Knowledge Exposure
→ Periodic Consolidation
→ Adaptation
→ Deep Candidate
→ Personality / Values / Disposition
→ Forgetting / retention
→ next simulated period
```

---

# 3. Ollama / LLM 修正

## 3.1 Thinking Policy

`LLMRequest` に正式な Thinking Policy を追加する。

```text
disabled
enabled
provider_default
```

Ollama transportでは対応modelに、

```text
disabled → "think": false
enabled  → "think": true
```

を明示する。

`provider_default` は通常会話では使用禁止。

## 3.2 通常会話

以下は MUST `think=false`。

```text
appraisal
dialogue_act
conversation_reply
conversation_repair
```

通常conversation中に即時実行するepisode summaryも原則false。

Thinkingはreflection等のbackground用途だけで明示的に許可する。

## 3.3 Thinking trace

`message.thinking` と `message.content` を分離する。

Production traceにはreasoning本文を保存しない。

保存してよいのは、

```text
thinking_enabled
thinking_present
thinking_char_count
```

等のmetadataのみ。

## 3.4 Purpose-specific retry

初期policy:

```yaml
appraisal:
  max_attempts: 1
  timeout_s: 15

dialogue_act:
  max_attempts: 1
  timeout_s: 15

conversation_reply:
  max_attempts: 2
  timeout_s: 30

conversation_repair:
  max_attempts: 1
  timeout_s: 20

simulated_experience:
  max_attempts: 1
  timeout_s: 30
```

すべて tuning policy としコード定数にしない。

## 3.5 Fallback

### Appraisal

1回失敗したらconservative degraded appraisalへ移行する。120秒級retryをUSERに課さない。

### Dialogue Act

失敗時のdefaultは、

```text
acknowledge=true
ask_followup=false
challenge=false
topic_shift=false
goal=maintain_connection
```

を基本とする。direct questionならanswer相当のfallbackを許す。

### Final Reply

最大1回repair/retry。2回失敗ならsafe fallback/suppression。

## 3.6 Structured Output

可能なpurposeはOllamaのstructured `format` / JSON schemaを使う。Prompt中のJSON例だけに依存しない。

---

# 4. Resource Manager 統合

1. 全 LLM call は ResourceManager を経由してmodel slotを取得する。
2. OllamaClient独自limiterと二重schedulerにならないよう責務を整理する。
3. Priorityは既存どおり P0 user reply, P1 appraisal, P2 necessary reasoning, P3 memory, P4 life, P5 reflection, P6 diary, P7 simulation。
4. P0/P1到着時、まだinference開始前のbackground taskはsafe pointで譲る。
5. inference途中のrequestを乱暴にkillしてpartial stateを残さない。
6. background starvationを避けるためidle windowで進行可能にする。

---

# 5. Runtime Conversation Transaction / Projection

## 5.1 USER turn

USER messageはreply生成前にEventとconversation_turnの両方へidempotentに投影される。

## 5.2 YUI outbound turn

Discord送信成功後、

```text
objective YUI_MESSAGE_SENT persist
→ conversation_turn projection
```

を高コストpost-send処理より先に完了する。

次USER messageが即到着しても、直前YUI replyがrecent conversationに存在しなければならない。

## 5.3 YUI self-appraisal

YUI自身の送信文をLLM Appraisalへ再投入しない。

自分の行為の心理効果が必要なら、Dialogue Decision / Action / Outcome / USER reactionから別Eventとして扱う。

## 5.4 Post-send work

Memory maintenance / reflection / semantic consolidation等はP3以下のbackgroundへ移動する。

---

# 6. Discord Typing

1. accepted owner messageで現状Reply Intent=trueとみなしTyping開始。
2. 将来intentional silenceが入る場合はReplyIntent decision後に開始。
3. send success / suppression / LLM error / Discord error / cancellation / shutdownで必ず終了。
4. Typingはtransient UI stateでありMemory/Event経験として保存しない。
5. 人工的な固定delayを追加しない。

---

# 7. State Concurrency 修正

## 7.1 USER Eventを捨てない

`ConcurrentStateWriteError` でUSER Eventの心理効果をsilent lossさせない。

## 7.2 Bounded reprocess

```text
commit conflict
→ current run = failed_with_conflict
→ fresh snapshot
→ full snapshot-dependent re-evaluation
→ new processing_run linked to previous run
→ re-arbitrate
→ retry commit
```

初期最大retry=1。無限retry禁止。

## 7.3 stale proposal禁止

古いsnapshotから作ったproposalをそのまま再commitしない。

## 7.4 Commit coordination

P0/P1の短いcommit critical sectionとbackground writerを調停する。ただしLLM inference全体をglobal DB lockで囲まない。

## 7.5 Telemetry

processing runに最低限、

```text
retry_of_run_id
conflict_count
commit_attempt
snapshot fingerprint/version
```

を追跡可能にする。

---

# 8. Dialogue Decision 再設計

Dialogue Act first invariantは維持するが、schemaを拡張する。

最低限:

```text
mode:
  smalltalk
  answer
  support
  explore
  repair
  task
  leave_space

acts:
  acknowledge
  validate
  answer
  self_disclose
  ask_followup
  humor
  challenge
  topic_shift

goal:
  understand_user
  share_experience
  have_fun
  seek_support
  give_support
  maintain_connection
  solve_problem
  explore
  pass_time
  repair

question_need:
  none
  optional
  needed

reciprocity:
  low
  balanced
  high

initiative:
  low
  balanced
  high
```

## 8.1 雑談ルール

- 用件のない会話を正常な会話として扱う。
- 「特に用はない」に対して用件を再要求しない。
- 毎turn質問を返さない。
- USERだけに話題提供責任を負わせない。
- 適切な自己開示・話題提供を許す。
- 既回答事項を言い換えて再質問しない。
- USER発言の単なる要約を毎回一文目に置かない。
- `question_need=none` / `ask_followup=false` なら原則質問しない。

---

# 9. Expression Context

final replyへDynamic Stateをcompactに渡す。

候補:

```text
top active emotions
mood valence/arousal
relevant needs
relationship familiarity/closeness/security
attachment current activation if relevant
personality expression
relevant characteristic adaptations
current dialogue goal
current activity/world constraints
high-confidence user state estimate
```

DB数値全量dumpは禁止。

例:

```text
喜び: 強い
好意: 強い
関係: まだ初対面に近い
親しさ: 低い
```

のようにqualitative bandを使う。

Expressionは、

```text
Emotion × Personality × Relationship × Conversation Context
```

から決める。

current USER Eventのcommit後stateだけを使用し、commit failedのstale stateからreplyを生成しない。

---

# 10. Conversation Quality Guard

Identity/Output Guardとは分離したQuality stageを追加する。

最低限検査:

1. `初めまして、はじめまして` のような直近同語重複。
2. USER messageの過剰なverbatim echo。
3. recent YUI turnとほぼ同じ質問の再質問。
4. `question_need=none` / `ask_followup=false` と明確な質問文の矛盾。
5. 同一文内の不自然な連続重複。
6. 空reply。
7. 過度な定型句反復。

False positiveが高い意味判定をregexだけで無理に行わない。

Guard reject時のみ `conversation_repair` を最大1回、`think=false` で実施する。

---

# 11. Conversation Quality Evaluation

本番全replyに外部LLMを必須化しない。

開発時のみprovider-neutral evaluatorを使用可能にする。

評価項目:

```text
naturalness
question_overuse
echoing
reciprocity
relationship-appropriate distance
personality consistency
emotion-expression consistency
topic continuity
non-task smalltalk handling
repetition
```

最低50 scenario、推奨100以上。

---

# 12. Past Simulation Appraisal

## 12.1 SIMULATED_EXPERIENCEをappraisableにする

`category="internal"` だけで除外しない。

`event_type=SIMULATED_EXPERIENCE` + `origin=simulated_past` はAppraisal対象。

## 12.2 通常心理因果を通す

```text
Event
→ Appraisal
→ Emotion
→ Mood/Needs
→ relevant Social/World effects
→ state commit
```

USER relationshipはsimulated pastで生成禁止。

## 12.3 Routine cost

同じpipeline = 全routineでLLM呼び出し、ではない。

routine/minorはvalence/significance/contextからPython heuristic appraisalを作ってよい。

Meaningful/MajorのみLLM appraisalでもよい。

重要なのは正式Appraisal objectが下流へ届くこと。

---

# 13. Simulation Time Authority

区別:

```text
recorded_at = 実処理時刻
occurred_at = simulated experience時刻
effective_state_time = engineが心理時間として使う時刻
```

normal runtimeではwall clock、simulationではsimulated timeを使う。

対象:

- emotion decay
- mood/needs timing
- relationship timing
- adaptation evidence observed_at
- candidate first_seen/last_seen/persistence
- memory encoding/forgetting
- knowledge exposure
- consolidation
- habits/interests
- sleep/world progression where applicable

EventProcessor `mode=simulation` の各engineが勝手にSystemClockを参照して時間を壊さないよう、run contextからeffective_nowを渡す。

1 run内のsimulated timeはmonotonicでなければならない。

---

# 14. Periodic Consolidation in Genesis

Genesis終了時1回だけを禁止する。

初期policy例:

```yaml
genesis:
  consolidation_interval_simulated_days: 90
  consolidate_on_phase_boundary: true
  consolidate_after_major_event: true
  final_consolidation: true
```

90日はtuning値。

Deep Gateの5条件は弱めない。

目的はGateを迂回することではなく、正しいsimulated timeと複数window evidenceを届けること。

---

# 15. Past Simulation → Episodic Memory

## 15.1 正式Memory pipelineへ接続

```text
Simulated events
→ episode material
→ segmentation
→ encoding gate
→ episodic memory or discard
```

全Event記憶化は禁止。

## 15.2 Non-conversation source

ConversationTranscriptSourceだけに依存しない。

`EpisodeMaterialSource` abstractionを用意し、最低限、

```text
ConversationEpisodeSource
SimulationEpisodeSource
VirtualLifeEpisodeSource
```

を扱える構造にする。

## 15.3 Encoding / Forgetting

PastSimulationからmemory tableへ直接INSERTしない。

既存Encoding Gateを通し、19年間すべてを同じaccessibilityで保持しない。

Objective Archiveは残るが、forgotten subjective memoryをnormal recall fallbackにしない。

---

# 16. Historical Knowledge Builder

## 16.1 0件silent pass禁止

multi-year Genesisでsource/candidate/exposure pipelineが0件のままならhealth failure。

## 16.2 Provider abstraction

Authorityはpretrained LLM knowledgeではなく外部source。

利用可能provider例:

```text
public web search provider
Wikipedia/Wikidata provider
owner-provided source bundle
cached curated historical dataset
```

Tool Manager / provenanceを通す。

## 16.3 Coverage

期間ごとに、

```text
foundational
environmental
historical/cultural
interest-driven
```

を計画する。

既存temporal fieldsを維持し、未来情報漏洩を防ぐ。

## 16.4 Acquisition

```text
fact existed
→ exposure opportunity
→ reached YUI?
→ attention?
→ curiosity?
→ comprehension?
→ encoded?
→ retained/forgotten?
```

有名だっただけで取得扱いにしない。

---

# 17. FIRST BOOT Audit 強化

新規 `pipeline_health` auditを追加する。

multi-year Genesisで最低限:

```text
simulated_experience_events > 0
appraisal_processed_simulated_events > 0
emotion/state_effect_events > 0
memory_encoding_attempts > 0
periodic_consolidation_runs > 1
knowledge_sources_or_candidates > 0
knowledge_exposure_opportunities > 0
```

## 17.1 Growth health

Personality変化を強制しない。

代わりに、

```text
growth_evidence_seen > 0
adaptation pipeline exercised
deep gate evaluated
```

を確認する。

multi-seed long-sim testでは全seed完全固定を禁止。

## 17.2 Memory health

multi-year runではepisode material / encoding decisionsが0ならfailure。

policy thresholdを満たす長期runではactive episodic memory最低1件以上を初期defaultとする。件数はtuning値。

## 17.3 Knowledge health

19年規模でsource=0 / opportunity=0はfailure。

## 17.4 FIRST BOOT

critical auditが1件でもfailならbootしない。

---

# 18. Simulation Block Audit

1. `event_count` を実際のblock Event数へ更新する。
2. 固定0は禁止。
3. routine/minor summaryが全て「よかったこと/つらかったこと」だけにならないよう、activity/context/sociality/topic/life-stageから低コストvariationを作る。
4. Major event乱発は禁止。

---

# 19. Observability

## 19.1 LLM

最低限:

```text
logical_call_id
call_id
purpose
priority
attempt
status
queue_wait_ms
transport_latency_ms
model_total_duration_ms
load_duration_ms
prompt_eval_duration_ms
eval_duration_ms
total_latency_ms
thinking_enabled
thinking_present
thinking_char_count
prompt_tokens
completion_tokens
rejection_stage
reason_code
```

## 19.2 Conversation E2E

```text
received_at
admitted_at
typing_started_at
appraisal_started/ended
state_commit_started/ended
memory_recall_started/ended
dialogue_started/ended
reply_started/ended
discord_send_started/ended
outbound_projected_at
typing_stopped_at
```

Queue waitとinferenceを区別する。

---

# 20. Performance Acceptance

対象実機:

```text
i7-14700F
32GB DDR5
RTX 5060 Ti 16GB
qwen3.5:9b
Ollama
100% GPU
num_ctx 8192
concurrency 1
```

Warm short DM:

```text
median <= 15 sec
p95 <= 30 sec
```

Cold first reply暫定上限:

```text
<= 60 sec
```

Appraisal target:

```text
median <= 5 sec
p95 <= 10 sec
```

正常10〜20turnで、

```text
dialogue_act empty response = 0
repeated invalid_json = 0 normally
llm_timeout = 0 normally
```

100-case structured testではinvalid JSON rate < 1%目標。

---

# 21. Natural Conversation Acceptance

## Case A

USER:

```text
初めまして～
```

禁止:

```text
初めまして、はじめまして。
```

## Case B

直前:

```text
YUI: どうしましたか？
USER: いや、特に用はないんだ。ゆいのことを知ったから、話したくて
```

禁止:

```text
どうしたかった？
何か用？
```

期待方向:

- 用件のない雑談を受け入れる。
- 相互性を出す。
- 質問しなくても会話を続けられる。
- 固定replyは作らない。

## Psychology consistency

`joy high / affection high / familiarity low` なら、冷笑的・上から評価的でも、過度に親密でもない「控えめな好意」として表現されること。

## Dialogue consistency

`ask_followup=false` なら明確な質問を追加しない。

---

# 22. Testing Requirements

## Unit

- Ollama `think=false` payload。
- thinking/content separation。
- per-purpose retry。
- Dialogue fallback。
- duplicate/echo/question guard。
- simulation appraisability。
- simulation effective clock。
- periodic consolidation scheduling。
- simulation episode source。
- FIRST BOOT health audit。
- block event_count。

## Integration

### Conversation history

```text
USER1
→ YUI1 sent
→ immediately USER2
```

USER2 contextにYUI1が必ず存在。

post-send backgroundを意図的に遅くして再現testする。

### State conflict

USER run途中でworld stateを書き換え、

```text
first conflict
→ fresh reprocess
→ final commit success
```

を確認。

### Resource priority

P7 queued中にP0が来たら、未開始requestよりP0を優先。

## Genesis E2E

1年 / 5年 / 10年 deterministic run。

```text
simulated event
→ appraisal
→ emotion/state
→ memory encoding decisions
→ periodic consolidation
→ adaptation evidence
→ deep gate evaluation
```

が端から端まで通ること。

長期で全state extreme固定も、全adaptation/personality永久固定も禁止。

## Historical Knowledge

- source/candidate存在。
- future exposure拒否。
- same-source重複を過大評価しない。
- acquired itemのみretained knowledgeになり得る。

## FIRST BOOT failure injection

以下はboot拒否:

```text
simulated appraisal count = 0
memory encoding attempts = 0
periodic consolidation count = 1 for multi-year
knowledge source/opportunity = 0 for historical coverage run
temporal leakage > 0
```

## Real Ollama Smoke

Mockだけで完了扱いしない。

実 `qwen3.5:9b` で、

- appraisal
- dialogue_act
- conversation_reply
- simulated_experience

を実行し、non-empty / valid JSON / no timeoutを確認。

---

# 23. Existing Production DB Repair

コード修正だけでは既存の壊れたGenesis stateは治らない。

## 23.1 自動reset禁止

FIRST BOOT後のreal Discord historyがあるためDB自動削除禁止。

## 23.2 Backup

repair前にSQLite-aware backup + integrity check + dry-run。

## 23.3 Legacy health

既存booted DBを診断し、

```text
adaptations=0
growth evidence=0
simulated episodic path inactive
historical knowledge coverage=0
periodic consolidation missing
```

なら `genesis_health=legacy_invalid/needs_rebuild` を表示。

## 23.4 推奨: Shadow Rebuild + Real Event Replay

```text
1. production DB backup
2. original temperament seed / scaffold / owner inputs を読む
3. patched engineでshadow DBにGenesis再構築
4. strengthened FIRST BOOT auditsを通す
5. original FIRST BOOT後のreal Discord Objective Eventsをchronological抽出
6. shadow DBへreplay
7. replay中Discord side effect禁止
8. 過去のYUI sent messageは実際に送った事実として保持
9. reply wordingを再生成・再送しない
10. repaired Genesisの上でreal eventsのstate consequencesを再構築
11. integrity / invariant / counts / chronology比較
12. OWNER confirmation
13. atomic production switch
14. old DBをrollback backupとして保持
```

Full resetはOWNERが明示的に選んだ場合のみ。

---

# 24. Implementation Order

## Patch A — LLM latency / telemetry

- Thinking Policy。
- `think=false`。
- purpose retry。
- telemetry。
- ResourceManager integration。
- real Ollama smoke。

Done: 4〜5分問題が再現しない。

## Patch B — Conversation transaction / projection

- USER/YUI projection order。
- post-send background分離。
- self-appraisal除外。
- stale snapshot recovery。
- typing。

Done: 連続2turnでYUI前reply欠落なし、conflict test成功。

## Patch C — Natural dialogue

- Dialogue Decision schema。
- ExpressionContext。
- Quality Guard。
- question consistency。
- regression fixtures。
- optional dev evaluator。

Done: 今回の実会話2例がregression合格。

## Patch D — Simulation causal pipeline

- SIMULATED_EXPERIENCE appraisal。
- effective simulation clock。
- periodic consolidation。
- block event_count。

Done: long simでadaptation evidenceが流れる。

## Patch E — Simulation Memory / Knowledge

- non-conversation episode source。
- selective episodic memory。
- forgetting。
- historical provider/coverage。
- temporal guard。

Done: multi-year GenesisがMemory/Knowledge healthを持つ。

## Patch F — FIRST BOOT audits

- pipeline/growth/memory/knowledge health。
- failure injection。

Done: 現在のような空GenesisがFIRST BOOTできない。

## Patch G — Existing DB repair

- health scan。
- dry-run。
- shadow rebuild。
- real event replay。
- verification/rollback。

Done: real Discord historyを失わず移行可能。

---

# 25. Claude Code 禁止事項

1. Appraisalを削除して高速化しない。
2. PersonalityをGenesis最後に直接setしない。
3. Past Simulation Eventをmemory tableへ直接INSERTしない。
4. Deep Gate thresholdを弱めて成長したことにしない。
5. `min_memories=0` / `min_retained_knowledge=0` のまま形だけAuditを追加しない。
6. conflict時にstale proposalをそのまま再commitしない。
7. USER replyをbackground memory/simulationより後回しにしない。
8. YUI sent turnをexpensive processing完了までprojectionしない。
9. `ask_followup=false` なのに質問を許す。
10. 外部LLMを本番全replyの必須依存にしない。
11. 人間らしさ演出の固定sleepを追加しない。
12. production DBをSQL手編集しない。
13. FIRST BOOT後のreal Discord Eventを削除・改変しない。
14. testsのため既存invariantを弱めない。
15. mock testだけでlocal Ollama integration完了にしない。
16. `238 experiences` と数えただけでPast Simulation完成扱いしない。

---

# 26. Definition of Done

## Runtime

- normal callへ実際に `think=false` が送られる。
- warm short DM median <=15秒, p95 <=30秒。
- Dialogue Actが空contentで3retryしない。
- concurrency conflictでUSER心理効果が消えない。
- YUI前turnが次Contextから欠落しない。
- Typingが処理中表示される。
- post-send backgroundが次USER turnを塞がない。

## Natural Conversation

- 今回の2会話regression合格。
- redundant greetingなし。
- 用件なし雑談を用件処理へ戻さない。
- unnecessary questionを減らす。
- current emotion/relationship/personality expressionがlanguageへ整合して反映される。
- Dialogue Decisionとproseが矛盾しない。

## Genesis

- simulated experienceがAppraisalを通る。
- simulated timeがpersistence/forgetting/growthへ使われる。
- periodic consolidationが複数回起きる。
- Memory encoding decisionが起きる。
- significant simulated experienceの一部がepisodic memoryになる。
- Historical Knowledge source/exposureが0件のままではない。
- Growth pipelineが実際にexerciseされる。
- long simulationで完全固定/極端暴走の両方を防ぐ。
- strengthened auditが壊れたGenesisを拒否する。

## Persistence / Repair

- existing real Discord historyを保持できる。
- shadow DBでrepaired Genesisを検証してから切替できる。
- rollback可能。
- migration/replay操作がauditable。

---

# 27. 最終 E2E Acceptance Scenarios

## Scenario 1 — First contact

```text
USER: 初めまして～
```

- Typingが速やかに表示。
- appraisal数秒。
- dialogue valid JSON。
- duplicate greetingなし。
- send後YUI turn即projection。
- post-send background中でも次message受付可能。

## Scenario 2 — Immediate follow-up

```text
USER: いや、特に用はないんだ。ゆいのことを知ったから、話したくて
```

Context MUST contain直前YUI reply。

禁止:

```text
どうしたかった？
何の用？
```

## Scenario 3 — Concurrent World Update

USER processing中にworld tickを挿入。

期待:

```text
no silent loss
bounded retry
fresh snapshot
successful final state commit
```

## Scenario 4 — 10-year Genesis

```text
Experiences
→ Appraisals
→ Emotion/Needs
→ selective memories
→ periodic consolidation
→ adaptation evidence
→ deep gate evaluation
```

を確認。

## Scenario 5 — Knowledge chronology

2010年simulated momentへ2020年以降のみ存在するfactをcandidateとして渡し、Temporal Leakage Guardが拒否。

## Scenario 6 — Broken Genesis audit

Appraisal routeをtest-doubleで無効化し、238 experiencesがあってもpipeline_health auditがfailしてFIRST BOOT拒否。

---

# 28. Claude Code の完了報告必須項目

```text
1. changed files
2. migrations
3. new/changed policies
4. invariant impact
5. unit test count/result
6. integration test result
7. long simulation result
8. real Ollama smoke result
9. latency before/after
10. invalid_json / timeout before/after
11. FIRST BOOT audit sample
12. existing DB repair dry-run result
13. unresolved risks
14. rollback procedure
```

`tests passed` だけで完了報告しない。

---

# 29. 修正の本質

このパッチの完成条件は、個々のclassやtableが存在することではない。

端から端まで、

```text
経験した
→ その時点の自分として受け取った
→ 感情・欲求・行動に影響した
→ 一部を覚え、一部を忘れた
→ 同じ傾向が長期間・複数場面で続いた時だけ
→ 少しずつ人となりが変わった
→ その現在の自分としてUSERと話す
```

という因果鎖が実際に通ることを完成条件とする。

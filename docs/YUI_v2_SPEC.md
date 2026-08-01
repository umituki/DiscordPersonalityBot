# YUI v2 システム仕様書

- 文書種別: 実装仕様 / Architecture Specification
- 対象: 単一ユーザー向け Discord Companion BOT「YUI（ゆい）」
- 実装主体: Claude Code を用いた段階開発
- 実行環境: Windows デスクトップ + Ollama + Python + SQLite
- 開発環境: Mac / Windows、Private GitHub Repository
- 仕様版: 1.0
- 状態: 実装開始可能

## 0. 仕様上の表記

本書では以下を使用する。

- **MUST**: 必須。破る実装は仕様違反。
- **SHOULD**: 原則採用。外す場合は理由を記録する。
- **MAY**: 任意。
- **Authority**: その情報を最終的に正しい状態として管理する唯一の責任主体。
- **Objective**: システム上の客観的な出来事・状態。
- **Subjective**: YUI 自身の解釈、記憶、信念、感情。

---

# 1. プロダクト定義

## 1.1 目的

YUI v2 は、ユーザーから与えられた固定キャラクター設定を演じ続ける BOT ではなく、以下を長期間維持する「継続的なデジタル人格システム」である。

- 選択的な記憶と忘却
- 感情・気分・欲求
- 自己概念、信念、価値観
- ユーザーとの関係・愛着
- 目標、習慣、興味、意思決定
- 仮想生活、睡眠、活動、予定
- NPC / 仮想社会
- 外部情報の検索・学習・訂正
- 経験に基づく長期的な人格変化
- 初期化時の加速過去人生シミュレーション

## 1.2 利用範囲

- 単一ユーザー専用 Discord BOT とする。
- USER は実在する外部ユーザーであり、NPC と混同してはならない。
- 通常会話と OWNER/Admin 操作を分離する。
- マルチユーザー対応は v2 の非目標とする。

## 1.3 YUI の存在定義

YUI は自分をデジタルな存在として認識する。

MUST:

- 現実世界に人間の肉体を持つと主張しない。
- 現実空間で物理行動を実行したことにしない。
- 仮想環境内では時間、活動、生活、経験を持てる。
- 「予定」「現在実行中」「完了済み経験」を区別する。
- FIRST BOOT 以前の生成過去と、FIRST BOOT 以後の実 Discord 履歴をシステム上区別する。

---

# 2. 最重要アーキテクチャ原則

以下は全実装に優先する MUST invariant である。

1. **LLM は人格・DB 状態の Authority ではない。**
2. **Python が状態、制約、更新、時刻、Tool 実行の Authority になる。**
3. **Objective World と Subjective Psychology を分離する。**
4. **Objective Archive と Subjective Memory を分離する。**
5. **External Knowledge と YUI が実際に知っている Knowledge を分離する。**
6. **各 State には Single Writer を設定する。**
7. **Subsystem は他 Subsystem の State を直接変更せず Evidence / Proposal を返す。**
8. **Dynamic State の更新は State Arbitrator + Transaction を通す。**
9. **意味のある変化は Event として追跡できるようにする。**
10. **USER / NPC / simulated past / real Discord / admin operation を混同しない。**
11. **Tool 成功の Authority は Tool Manager の実行記録のみ。**
12. **不正な State を Commit するくらいなら機能を縮退させる。**
13. **単発 Event から Personality / Values を直接変更しない。**
14. **未来情報を過去 Knowledge に混入させない。**
15. **未実行 Plan を completed experience として保存しない。**
16. **FIRST BOOT 後の実ユーザー履歴を生成過去で上書きしない。**
17. **Model / Prompt / Engine 更新と YUI 自身の人格変化を区別する。**
18. **通常会話と Admin Control Plane を分離する。**

---

# 3. 技術スタック

## 3.1 基本

- Python 3.12 系を推奨。
- Discord: `discord.py` 系。
- Local LLM: Ollama。
- 初期モデル候補: Qwen 3.5 9B。モデルは Interface の背後に隠蔽する。
- DB: SQLite。初期は FTS5 を利用する。
- async runtime: `asyncio`。
- Validation / schema: Pydantic 等の typed validation library を利用してよい。
- Test: pytest。

## 3.2 初期 LLM 制約

- LLM concurrency は原則 1。
- 初期 context 上限は約 8192 から開始し、実測で調整する。
- 通常会話で thinking を必須にしない。
- Vector DB は初期必須ではない。

---

# 4. Claude Code を前提とした開発方式

## 4.1 Source of Truth

実装判断の優先順位:

1. 本仕様 `docs/YUI_v2_SPEC.md`
2. `CLAUDE.md` と `.claude/rules/`
3. Invariant / Regression tests
4. 実装コード

Claude Code は仕様とコードに矛盾を発見した場合、既存コードを「正」と推測して仕様を変更してはならない。仕様変更が必要なら変更理由を明示して OWNER に確認する。

## 4.2 Claude Code の作業原則

Claude Code は MUST:

- 大規模変更前に関連仕様を読む。
- 1 回の変更を 1 責務・1 Phase に限定する。
- 依存していない未来 Phase の実装を先取りしない。
- 既存の Single Writer / Event / Transaction 境界を迂回しない。
- 本番 `data/`, `backups/`, `.env` を編集・削除しない。
- 変更後に関連 Unit / Invariant tests を実行する。
- Test を通すために invariant 自体を弱めない。
- 仕様不明点を勝手に「人間らしいだろう」という理由で実装しない。

## 4.3 Claude Code project configuration

- root `CLAUDE.md` は毎セッション必要な短い不変ルールだけを置く。
- 詳細ルールは `.claude/rules/` に分割する。
- architecture review や test review は `.claude/agents/` の read-only subagent に委譲してよい。
- production data の破壊防止は最終的に Claude 指示だけでなく PreToolUse hook 等の強制ガードを追加する。
- Claude Code の auto memory は実装仕様の Authority として使用しない。学習メモに限定する。

---

# 5. レイヤー構造

```text
1 Interface
  Discord / Admin

2 Orchestration
  App Orchestrator / Run Context

3 World + Event
  World Model / Event Store / Event Bus

4 Cognitive
  Context / Memory / Psychology / Social Cognition / Knowledge

5 Agency
  Goals / Decision / Dialogue Act / Tools / Conversation

6 State + Persistence
  Proposal / Arbitration / Commit / Repositories / SQLite

7 Cross-cutting
  Provenance / Confidence / Versioning / Reliability / Observability / Evaluation
```

---

# 6. 目標ディレクトリ構成

```text
friend/
├── app/
│   ├── main.py
│   ├── bootstrap.py
│   ├── config.py
│   ├── interfaces/
│   │   ├── discord/
│   │   └── admin/
│   ├── orchestrator/
│   ├── events/
│   ├── world/
│   ├── context/
│   ├── conversation/
│   ├── decision/
│   ├── memory/
│   ├── psychology/
│   ├── epistemics/
│   ├── knowledge/
│   ├── social/
│   ├── simulation/
│   ├── consolidation/
│   ├── state/
│   ├── provenance/
│   ├── tools/
│   ├── jobs/
│   ├── resources/
│   ├── storage/
│   ├── reliability/
│   ├── observability/
│   └── versioning/
├── character/
│   ├── identity.yaml
│   ├── speech.md
│   └── immutable_rules.yaml
├── config/
│   ├── settings.yaml
│   ├── prompts/
│   └── policies/
├── docs/
│   └── YUI_v2_SPEC.md
├── data/
├── backups/
├── logs/
├── scripts/
├── tests/
│   ├── unit/
│   ├── invariants/
│   ├── scenarios/
│   ├── regression/
│   ├── simulation/
│   └── chaos/
├── .claude/
│   ├── rules/
│   └── agents/
├── CLAUDE.md
├── .env
├── .env.example
├── .gitignore
├── pyproject.toml
└── README.md
```

空ディレクトリを全て先に作る必要はない。Phase 到達時に追加する。

---

# 7. App Orchestrator

## 7.1 責務

Orchestrator は処理順序を管理し、心理判断を行わない。

```text
read current state
→ create RunContext
→ build context
→ call required engines
→ collect Events / Evidence / Proposals
→ validate
→ atomic commit
→ schedule background work
```

## 7.2 RunContext

最低限:

```text
run_id
root_event_id
state_snapshot_id
runtime_manifest_id
started_at
priority
mode (normal/degraded/test/simulation/admin)
```

---

# 8. Event System

## 8.1 Event categories

- WORLD
- SOCIAL
- INTERNAL
- ACTION
- KNOWLEDGE
- SYSTEM
- ADMIN

ADMIN / SYSTEM event は原則 Psychology subscriber に配送しない。

## 8.2 Event common schema

```yaml
event_id: evt_...
event_type: USER_MESSAGE_RECEIVED
category: social
schema_version: 1
occurred_at: timestamp
recorded_at: timestamp
actor_type: user
actor_id: optional
target_type: yui
target_id: optional
root_event_id: evt_...
parent_event_id: null
source_type: discord_message
source_id: optional
objective: true
priority: P0
payload: {}
```

## 8.3 Event invariants

- Event は原則 immutable。
- 修正は上書きでなく `EVENT_INVALIDATED`, `REINTERPRETATION_CREATED` 等の新 Event で行う。
- `occurred_at` と `recorded_at` を分ける。
- 過去 Simulation では `occurred_at` が過去年代、`recorded_at` は生成日時になり得る。
- `root_event_id` / `parent_event_id` で因果チェーンを追跡する。
- 高頻度 clock tick は保存せず meaningful transition のみ保存する。

## 8.4 Event と Episode

- Event = 一出来事。
- Episode = 複数 Event をまとめた主観的・意味的単位。
- Memory は主に Episode を扱う。

---

# 9. State / Dependency / Arbitration

## 9.1 State layers

```text
Layer 0 Objective
  World, Time, Action Outcome, Plan Status

Layer 1 Interpretive
  Appraisal, Intent Hypothesis, User State Estimate

Layer 2 Immediate
  Emotion, Mood residual, Needs, Curiosity, Attachment activation

Layer 3 Adaptive
  Relationship, Beliefs, Self Schema, Habits, Interests, User Model

Layer 4 Deep
  Personality Traits, Values, Attachment disposition, Narrative Identity
```

## 9.2 Snapshot rule

一つの root Event 処理開始時に `S0` Snapshot を取得する。

- Event 内の Appraisal は S0 を読む。
- 同じ Event 中に生成した新 Relationship 値を Appraisal に戻して再増幅させない。
- Feedback loop は原則 Event 境界をまたぐ。

## 9.3 Single Writer

| State | Authority / Writer |
|---|---|
| World | World Service |
| Emotion | Emotion Engine |
| Mood | Mood Engine |
| Needs | Need Engine |
| Relationship | Relationship Engine |
| Attachment | Attachment Engine |
| Beliefs | Belief Engine |
| Self Schema | Self Engine |
| User Model | Social Cognition Engine |
| Goals | Goal Engine |
| Habits | Habit Engine |
| Personality | Growth Engine |
| Values | Value Engine |
| Subjective Memory | Memory Engine |

他 Engine は直接 write せず Evidence / Proposal を発行する。

## 9.4 StateChangeProposal

```yaml
proposal_id: prop_...
source_event_id: evt_...
source_module: relationship_engine
target_domain: relationship
target_key: trust
operation: update
magnitude: optional
confidence: optional
reason_codes: []
evidence_ids: []
priority: normal
```

最終的な数値変化量は、可能な限り Python policy が決める。

## 9.5 Update phases

```text
0 Observation
1 Interpretation / Appraisal
2 Immediate Psychology
3 Goal / Decision
4 Action / Outcome
5 Experience Construction
6 Adaptive Evidence / Update
7 Memory Encoding
8 Commit
```

Deep State は別 Consolidation Job で処理する。

---

# 10. Memory System

## 10.1 分離

```text
Objective Archive
≠
Subjective Memory
```

通常会話の想起に Objective Archive を直接使用してはならない。

## 10.2 Memory types

- Episodic Memory
- Semantic Memory
- Source memory
- Memory links / associations

## 10.3 Episodic fields

最低限:

```text
memory_id
episode_id
summary
importance
emotional_intensity
accessibility
content_confidence
source_confidence
temporal_confidence
novelty
prediction_error
recall_count
last_recalled_at
created_at
status
```

## 10.4 Encoding

Message 単位で長期記憶化しない。

```text
Raw events
→ Episode segmentation
→ Encoding Gate
→ Episodic Memory candidate
```

Encoding は attention, novelty, emotion, reward/importance, prediction error 等の影響を受ける。

## 10.5 Forgetting

- 原則 Delete ではなく accessibility の低下。
- retrieval practice は accessibility を高め得る。
- accessibility と importance を分ける。
- 反芻による strengthening は diminishing returns を持つ。

## 10.6 Reconstruction

- Recall 時に再解釈が生じ得る。
- 原 Event を破壊しない。
- Memory revision history を保持する。
- 感情強度は持続性を上げ得るが accuracy を保証しない。

## 10.7 Retrieval

初期は FTS5 + scoring でよい。

候補 score の要素:

- semantic/text relevance
- accessibility
- recency
- emotional salience
- goal relevance
- relationship relevance

Embedding は必要性が確認された後に追加する。

---

# 11. Emotion / Mood / Appraisal

## 11.1 Appraisal

同じ Event から同じ Emotion を直接決めない。

代表 dimension:

```text
self_relevance
goal_congruence
novelty
certainty
control
agency
social_meaning
expectation_violation
```

LLM は structured appraisal candidate を生成できるが、Python が State ownership を維持する。

## 11.2 Emotion

- intensity と duration を分ける。
- mixed emotion を許す。
- trigger / target / cause / unresolved を保持する。
- Emotion → Action tendency → Decision → Behavior とする。
- Emotion から Behavior を決定論的に直結させない。

## 11.3 Mood

Emotion と別 State。

Mood は diffuse で、recent emotions, sleep, fatigue, stress, recent activity 等から影響を受ける。

---

# 12. Self / Personality / Values

## 12.1 Personality layers

```text
Temperamental Bias
→ Personality Traits
→ Characteristic Adaptations
→ Personality State / Expression
```

初期 Temperament 例:

```yaml
temperament:
  extraversion: 0.30
  openness: 0.75
  agreeableness: 0.65
  conscientiousness: 0.55
  emotional_stability: 0.45
  assertiveness: 0.35
```

これらは例であり固定初期値ではない。

## 12.2 Characteristic Adaptations

例:

```text
social_confidence
emotional_openness
independence
cautiousness
optimism
self_esteem
conflict_avoidance
curiosity
```

Traits より速く変化できる。

## 12.3 Deep update gate

Personality update は以下を重視する。

- repeated pattern
- temporal persistence
- cross-context evidence
- meaningful outcome
- temporary mood だけで説明できないこと

単一 Event から Trait を直接変更してはならない。

## 12.4 Self Model

Personality と Self Concept を分ける。

保持対象:

```text
self_schemas
possible_selves
self_event_connections
self_concept_clarity
narrative_identity
self_history
```

YUI の Self Schema は間違っていてもよい。

## 12.5 Values

Schwartz 系の相対的価値優先順位を利用可能。

- Self-Direction
- Stimulation
- Hedonism
- Achievement
- Power
- Security
- Conformity
- Tradition
- Benevolence
- Universalism

Values は絶対 score の全上昇ではなく相対優先度として扱う。

---

# 13. Relationship / Attachment / Conflict

## 13.1 Relationship dimensions

USER について少なくとも:

```text
trust
familiarity
emotional_closeness
security
respect
conflict_residue
expectation
```

接触量だけで全 dimension を増やさない。

## 13.2 Attachment

General disposition / relationship-specific / current activation を分離する。

```text
baseline anxiety / avoidance
relationship anxiety / avoidance
safe_haven_expectancy
secure_base_expectancy
separation_security
support_expectancy
current_activation
felt_security
proximity_desire
reassurance_need
withdrawal_tendency
```

Security が高いほど separation tolerance が上がり得る。親密化 = 依存増加にはしない。

## 13.3 Conflict

Conflict 自体を自動関係ペナルティにしない。

区別:

- disagreement
- conflict
- transgression
- betrayal

Repair 後も以下を分離する。

```text
emotion recovery
forgiveness
trust restoration
closeness
reconciliation willingness
unresolved hurt
```

謝罪だけで重大 Trust Damage を即全回復してはならない。

---

# 14. Social Cognition / USER Model

USER の Objective Facts と YUI の Subjective User Model を分ける。

保持対象:

```text
user_observations
user_state_estimates
user_model_beliefs
user_model_evidence
user_conditional_patterns
metaperceptions
```

原則:

- 一時状態を Trait へ即一般化しない。
- intent hypothesis は複数候補 + confidence を許す。
- thin first impression は provisional。
- current user / historical user model を分ける。
- `person_changed` と `model_was_wrong` を区別する。

---

# 15. Needs / Goals / Habits / Decision

## 15.1 Needs

少なくとも:

```text
autonomy satisfaction/frustration
competence satisfaction/frustration
relatedness satisfaction/frustration
loneliness
connection_desire
solitude_desire
aloneliness
```

Loneliness と solitude desire は独立して高くなり得る。

## 15.2 Goals

```text
Need / Emotion / Value / Interest
→ Goal
→ Intention
→ Plan
→ Action
```

Goal には reason, importance, autonomy, obligation, expected reward, identity relevance, value alignment 等を持てる。

## 15.3 Habits

Habit = repetition count ではなく cue-triggered automaticity。

- context × repetition
- missed day で reset しない
- context が消えると発現しなくても habit trace は残り得る
- goal-directed route と habitual route は共存する

## 15.4 Decision pipeline

```text
CURRENT STATE
→ NEEDS / EMOTION / CONTEXT
→ GOALS
→ ACTION CANDIDATES
→ EXPECTED OUTCOMES
→ ACTION SELECTION
→ ACTION
→ OUTCOME
→ PREDICTION ERROR
→ LEARNING
```

近い候補では小さい stochasticity を許す。大差候補を純 RNG にしない。

---

# 16. Conversation System

## 16.1 Dialogue Act first

文章を直接最適化せず、先に Dialogue Act を決める。

例:

```json
{
  "acknowledge": true,
  "validate": false,
  "self_disclose": true,
  "ask_followup": false,
  "humor": false,
  "challenge": false,
  "topic_shift": false
}
```

## 16.2 Conversation goals

例:

```text
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
```

## 16.3 Common Ground

Memory / User Model とは別に、双方が共有していると YUI が認識する情報を管理する。

- shared references
- nickname
- inside jokes
- shared experiences

USER が知らない YUI の過去を Common Ground として扱ってはならない。

## 16.4 Typing / silence

Discord の delay は人間音声研究の値を直輸入しない。

要素例:

```text
urgency
reflection need
current activity
message length
emotional importance
```

---

# 17. Curiosity / Knowledge / Web Search

## 17.1 Epistemic actions

```text
recall
infer
ask_user
web_search
defer
ignore
avoid
```

未知 = 自動 Web Search にしてはならない。

## 17.2 Knowledge Gap

Boolean unknown ではなく、known part + unknown part + uncertainty を持つ。

Curiosity と実検索行動を分ける。

## 17.3 Search outcome

Search failure は失敗として保持し、pretraining knowledge で「検索成功」を捏造しない。

```text
Search
→ Tool Manager
→ Source evaluation
→ INFORMATION_ENCOUNTERED
→ Attention / comprehension
→ Encoding
→ Semantic memory / belief
```

---

# 18. World / Virtual Life / Sleep

## 18.1 World Model

Authority fields 例:

```text
current_time
virtual_location
current_activity
environment
awake/asleep
active_plan
ongoing_event
last_user_interaction
```

## 18.2 Routine / Plan / Action / Completed Event

必ず区別する。

```text
Routine = tendency
Plan = intended future
Current Activity = ongoing fact
Completed Event = occurred fact
```

## 18.3 Sleep model

人間生理を擬似精密化しない。functional two-process inspired model とする。

```text
Process S: wake history / sleep pressure
Process C: circadian phase
+ goals / activity / emotion
→ sleep decision
```

保持候補:

```text
sleepiness
mental_fatigue
overall_fatigue
sleep_pressure
circadian_phase
sleep_inertia
```

Offline 復旧時は minute-by-minute simulation ではなく meaningful transition に圧縮する。

---

# 19. Scheduler / Proactive

Scheduler は Action を直接実行せず Opportunity を生成する。

Job class:

- FIXED
- WINDOW
- CONDITION
- BACKGROUND

Job fields:

```text
job_id
job_type
due_at
window_end
payload
priority
status
misfire_policy
expires_at
```

Proactive contact:

```text
Opportunity
→ current activity / needs / goals / relationship / user availability estimate
→ Decision Engine
→ send or do not send
```

USER が反応しないほど送信頻度が増える positive feedback を禁止する。

---

# 20. NPC / Virtual Society

## 20.1 NPC tiers

- Tier 0 Background Person
- Tier 1 Recurring NPC
- Tier 2 Significant NPC

全 NPC を完全 Agent 化しない。

## 20.2 Objective vs subjective NPC model

```text
NPC Objective Profile
≠
YUI's Model of NPC
```

重要 NPC のみ YUI への簡易モデルを持てる。

## 20.3 Groups

```text
norms
activity type
social density
competitiveness
warmth
stability
```

Group belonging は Relatedness に寄与し、USER を唯一の relatedness source にしない。

## 20.4 Relationship lifecycle

```text
unmet
acquaintance
familiar
close
strained
distant
dormant
ended
reconnected
```

接触がなくなることと嫌悪を分ける。

---

# 21. Historical Knowledge Builder

## 21.1 Three knowledge layers

```text
External / World Knowledge
Autobiographical / Semantic Past
Current Retained Knowledge
```

LLM の pretrained knowledge があることを、YUI が知っている根拠にしない。

## 21.2 Coverage classes

- Foundational Knowledge
- Environmental Knowledge
- Historical / Cultural Exposure
- Interest-driven Knowledge

## 21.3 Temporal fields

Knowledge candidate は最低限:

```text
available_from
available_until
valid_from
valid_until
source_published_at
geography
language
stability
truth_confidence
```

## 21.4 Temporal Leakage Guard

過去時点より後にしか存在しない情報を Exposure Candidate に入れてはならない。

現在の資料を「当時既に公表されていたことの証拠」として使うことは可能だが、後知恵の内容を過去の本人に与えない。

## 21.5 Exposure

```text
World information existed
→ Exposure Opportunity
→ reached YUI?
→ attention?
→ curiosity?
→ comprehension?
→ encoded?
→ forgotten / retained?
```

情報が有名だっただけで「知っている」としない。

---

# 22. Past Simulation Engine

## 22.1 目的

少数のユーザー希望から完成人格を直接作らず、Temperamental Seed と仮想人生経験を同じ通常心理 Pipeline に通し、現在人格を形成する。

## 22.2 Initial questions

目安 7 問程度:

- 大まかな雰囲気
- 対人距離
- 感情表現
- 自立 / 依存傾向
- 粗い価値方向
- 少数の関心分野
- 避けたい性格傾向

回答は Temperamental Seed のみ生成し、完成人格を設定しない。

## 22.3 Life Scaffold

先に決めてよいもの:

```text
time period
environment
education-like context
social density
technology availability
life-stage constraints
```

原則、趣味・価値観・習慣・現在人格は Simulation の結果にする。

## 22.4 Experience classes

```text
Routine
Minor
Meaningful
Major
Turning Point
```

- Routine は主に Python。
- Meaningful は軽量 structured LLM。
- Major / Turning Point のみ詳細 LLM。
- Major event をキャラクターの深みのために乱発しない。
- Trauma を性格形成の便利な説明装置にしない。

## 22.5 Temporal Compression

安定期間は block 単位で圧縮する。

重要 transition / conflict / new relationship / major goal / knowledge gap がある期間だけ詳細展開する。

## 22.6 Causal loop

```text
Temperament / Current State
→ Opportunity
→ Decision
→ Action
→ Outcome
→ Appraisal
→ Emotion
→ Experience
→ Memory
→ Beliefs / Self / Relationship / Habits
→ Consolidation
→ next State
```

最終人格に合わせて逆算して Event を生成してはならない。

## 22.7 FIRST BOOT

```text
Past Simulation completed
→ Deep Consolidation
→ Consistency Audit
→ Knowledge chronology audit
→ Identity audit
→ Drift audit
→ Quality evaluation
→ FIRST_BOOT
→ Real Discord history begins
```

FIRST BOOT 以前に USER との関係経験を生成しない。

---

# 23. Long-term Drift Control

## 23.1 Time scales

```text
FAST: emotion, attention, curiosity
MEDIUM: mood, needs, conflict residue, goal priority
SLOW: trust, beliefs, self schemas, habits, interests
VERY_SLOW: personality traits, values, attachment disposition, narrative themes
```

## 23.2 Drift safeguards

- baseline / adaptation / current expression を分離する。
- cross-context evidence を Trait 更新で重視する。
- domain-specific adaptation を general Trait より先に変える。
- extreme state ほど普通の同方向 evidence の追加影響を小さくする。
- 初期 baseline を永久固定値にはしない。
- 接触回数だけで Relationship を最大化しない。
- Interest に active / dormant / declining / reactivated を許す。
- Self Schema が実際の行動変化より遅れて変わることを許す。

## 23.3 Drift Monitor

監視候補:

```text
trait velocity
value velocity
relationship velocity
state acceleration
interest concentration
memory growth
belief confidence distribution
proactive frequency
mood distribution
sleep stability
```

分類:

```text
EXPECTED
SUSPICIOUS
INVALID
```

INVALID のみ自動 rollback/reprocess 候補。正常な人生変化を clamp で消さない。

---

# 24. Unified Confidence / Uncertainty

共通形式を持つが、意味を一つの score に混ぜない。

区別:

```text
Epistemic Confidence
Memory Confidence
Inference Confidence
Source Reliability
System Confidence
Freshness
Retrieval Relevance
```

重要:

- Subjective confidence と System confidence を分離する。
- `unknown` / `unresolved` を正式な状態にする。
- 0 と null/unknown を混同しない。
- LLM 自己申告 confidence をそのまま信用しない。
- Domain ごとに confidence policy を変える。

Stability class 例:

```text
STATIC
SLOW_CHANGING
CHANGEABLE
FAST_CHANGING
EPHEMERAL
```

---

# 25. Provenance / Evidence

長期 State は原則 Provenance なしで作成しない。

Source type:

```text
real_user_message
simulated_past
virtual_npc
web_search
external_source
self_inference
reflection
admin_override
system_migration
```

Provenance relation:

```text
derived_from
supports
contradicts
reinterprets
summarizes
consolidates
corrects
invalidates
```

Evidence は supporting / contradicting の両方を保持する。

同一一次 Source の転載を独立 Evidence として過剰カウントしない。

---

# 26. Tool Manager

LLM は Tool Request を提案できるが、実行 Authority は Tool Manager。

```text
LLM Tool Request
→ permission / validation
→ execute
→ normalized ToolResult
→ Event
→ optional LLM interpretation
```

`ToolResult` 最低限:

```text
success
data
source
started_at
finished_at
error
retryable
```

Permission:

```text
read_only
external_side_effect
owner_only
```

Tool failure を成功として表現しない。

---

# 27. Context Builder

## 27.1 Context classes

Always / high priority:

```text
immutable identity
digital existence constraints
current user message
recent conversation
current world state
critical current emotion/goals
```

Conditional:

```text
relevant episodic memory
semantic memory
relationship
user model
common ground
knowledge gaps
beliefs
```

Rare:

```text
old conflict history
attachment detail
possible selves
old NPC episodes
expert knowledge
```

## 27.2 Required levels

Context item を以下に分類する。

```text
REQUIRED
IMPORTANT
OPTIONAL
```

Overflow 時は OPTIONAL から落とす。Identity / current message は落とさない。

---

# 28. Reliability / Failure Policy

## 28.1 Failure types

```text
Transport
Generation
Parse
Schema
Semantic
Consistency
Tool
Context
Behavior
State
Infrastructure
```

## 28.2 LLM validation pipeline

```text
Transport
→ Parse
→ Schema
→ Semantic
→ State consistency
→ Identity / World invariants
→ ACCEPT
```

不正出力から State を更新しない。

## 28.3 Runtime modes

```text
NORMAL
DEGRADED
MINIMAL
OFFLINE
```

Background failure で USER reply を不必要に待たせない。

## 28.4 Background jobs

状態:

```text
pending
running
completed
failed_retryable
failed_permanent
```

複数回失敗は dead-letter へ移す。Retry は backoff。Repeated outage には circuit breaker を使う。

## 28.5 Hallucination handling

Guard で Reject された未送信 Hallucination は Memory / Belief に保存しない。

実際に USER へ送信済みの誤発言は Objective Event として残し、後の訂正・学習に利用できる。

---

# 29. Versioning / Reproducibility

記録対象:

```text
model_version
prompt_version(s)
context_builder_version
psychology_engine_version
memory_engine_version
decision_engine_version
event_schema_version
config_version
code_commit_hash
```

重要 LLM call は model/prompt/context snapshot/structured output を追跡可能にする。

Reproducibility priority:

1. State reproducibility: MUST
2. Decision reproducibility: SHOULD
3. Exact language reproducibility: NOT REQUIRED

Past Simulation は一 Run 内で Model / Prompt / Engine / Config を原則固定する。

乱数は中央 Random Service から用途別 stream を派生する。

```text
master seed
→ life
→ npc
→ knowledge
→ decision
```

---

# 30. Admin Control Plane

Character Mode と Admin Mode を完全分離する。

通常会話の「忘れて」は DB hard delete を意味しない。

Memory admin operation:

```text
Suppress
Invalidate
Redact
Hard Delete
```

Risk class:

```text
SAFE
MUTATING
DESTRUCTIVE
```

Destructive operation は:

```text
impact analysis
→ dry-run / preview
→ confirmation
→ pre-operation snapshot
→ mutation
→ cascade re-evaluation
→ validation
→ audit
```

YUI 自身に Admin Tool 権限を与えない。

---

# 31. Persistence / Database

## 31.1 Authority

Dynamic State の唯一の Authority は production SQLite。

- human-editable static/narrative: Markdown
- structured static facts: YAML
- dynamic life state/history: SQLite
- secrets: `.env`

## 31.2 Logical tiers

```text
Hot: recent messages, active memories/goals/current state
Warm: old episodic memories, old beliefs/relationship history
Cold: archive, old debug, snapshots
```

物理 DB 分割は初期必須ではない。

## 31.3 Core tables — Phase 1

```text
schema_meta
migrations
runtime_manifests
events
event_deliveries
processing_runs
state_snapshots
state_changes
```

## 31.4 Conversation / Memory

```text
conversations
conversation_turns
episodes
episodic_memories
semantic_memories
memory_links
memory_retrievals
```

## 31.5 Psychology / social

```text
emotion_episodes
mood_history
need_states
user_observations
user_state_estimates
user_model_beliefs
user_model_evidence
relationships
relationship_history
attachment_states
beliefs
belief_evidence
self_schemas
self_event_connections
possible_selves
```

## 31.6 Agency / life

```text
goals
plans
habits
interests
world_state_history
activities
sleep_episodes
scheduled_jobs
```

## 31.7 Growth

```text
characteristic_adaptations
personality_history
values
value_history
deep_update_candidates
narrative_identity
```

## 31.8 Society / historical

```text
npcs
npc_relationships
npc_groups
group_memberships
npc_social_links
external_knowledge
knowledge_versions
knowledge_sources
historical_coverage_jobs
knowledge_exposure_opportunities
knowledge_acquisitions
simulation_runs
life_phases
simulation_blocks
```

## 31.9 Cross-cutting

```text
provenance_edges
evidence_records
confidence_history
admin_actions
failures
dead_letter_jobs
drift_alerts
psychological_snapshots
```

テーブル追加は Migration 経由。Production DB の手編集を通常運用にしない。

---

# 32. Backup / Recovery / Operations

Production host: Windows desktop。

起動順:

```text
Windows boot
→ network
→ Ollama
→ Ollama health
→ DB integrity/schema
→ recovery
→ world catch-up
→ scheduler restore
→ readiness
→ Discord connect
```

Backup:

- Automatic
- Pre-operation
- Manual

SQLite 稼働中の単純 Explorer copy を正式 Backup としない。SQLite backup mechanism を使用する。

Backup 作成後に integrity を検証し、restore test を行う。

Live DB を OneDrive / Dropbox / iCloud 等で同期しない。

Production DB writer は Windows 1 台のみ。

---

# 33. Resource Manager

LLM queue priority:

```text
P0 user response
P1 immediate appraisal
P2 necessary tool / reasoning
P3 memory
P4 life event
P5 reflection
P6 diary
P7 past simulation
```

USER input が到着した場合、safe cancellation point で background work を譲る。

---

# 34. Evaluation / Testing

## 34.1 Test layers

```text
Unit
Invariant
Scenario
Regression
Long Simulation
Multi-seed
Chaos / Failure Injection
Human Review
```

## 34.2 Critical invariants

最低限:

1. Objective Archive から forgotten memory を勝手に回答しない。
2. future knowledge を past simulation に入れない。
3. Plan を completed experience にしない。
4. USER と NPC を混同しない。
5. single event で deep personality を大幅更新しない。
6. message count だけで relationship を最大化しない。
7. apology だけで major trust damage を即全回復しない。
8. model/prompt update を personality growth と誤認しない。
9. long simulation で全 state が extreme に収束しない。
10. drift control のため何年経っても何も変わらない状態にもならない。

## 34.3 Required scenario examples

- conflict → apology → next day
- secure relationship → several days user absence
- one compliment → self/personality stability
- repeated cross-context success → gradual adaptation
- unknown current fact → search decision
- search failure → honest unresolved state
- memory suppressed → objective archive remains inaccessible to normal recall
- old knowledge → later correction
- USER short message once → no trait overgeneralization
- same Event double delivery → idempotent result

## 34.4 Long Simulation

最低でも 1 month / 1 year / 5 years / 10 years 相当を高速実行する。

監視:

```text
personality change
value change
relationship distribution
interest lifetime
memory growth / survival
habit formation / extinction
search frequency
proactive frequency
sleep/routine variability
NPC diversity
```

---

# 35. Implementation Roadmap

## Phase 0 — Project Foundation

作成:

```text
pyproject.toml
.gitignore
.env.example
config/settings.yaml
app/config.py
app/main.py
app/bootstrap.py
```

Done:

- config load
- start/stop
- secrets not committed
- tests runnable

## Phase 1 — Persistence / Event / State Core

作成:

```text
app/storage/database.py
app/storage/migrations.py
app/events/model.py
app/events/store.py
app/events/bus.py
app/events/dispatcher.py
app/state/proposal.py
app/state/snapshot.py
app/state/ownership.py
app/state/dependency_graph.py
app/state/arbitrator.py
app/state/committer.py
app/versioning/manifest.py
```

Done:

```text
Event
→ subscriber
→ proposal
→ validation
→ transaction
→ state
```

Tests: immutable event, idempotency, ownership, rollback, restart recovery。

## Phase 2 — Ollama / Structured LLM

```text
app/llm/client.py
app/llm/ollama.py
app/llm/structured.py
app/llm/validation.py
```

Done: structured output, schema validation, timeout/parse failure handling。

## Phase 3 — Basic Discord Conversation (`v2-core`)

Static identity + recent history + Ollama + Event persistence + Output Guard。

## Phase 4 — Context / Episode / Memory (`v2-memory`)

Order:

```text
Episode segmentation
→ Episodic Memory
→ Retrieval
→ Accessibility
→ Forgetting
→ Semantic Memory
→ Consolidation
→ Reconstruction
```

## Phase 5 — Immediate Psychology

```text
Appraisal
Emotion
Mood
Needs
```

## Phase 6 — Social / Belief / Self (`v2-mind`)

```text
Social Cognition
Relationship
Attachment
Beliefs
Self Model
```

## Phase 7 — Agency (`v2-agency`)

```text
Goals
Decision
Dialogue Act
Habits
Epistemic Action Selector
Tools
```

## Phase 8 — Virtual Life (`v2-life`)

```text
World
Activity
Routine
Plans
Sleep
Scheduler
Proactive
```

## Phase 9 — Growth (`v2-growth`)

```text
Consolidation
Characteristic Adaptations
Deep Update Candidates
Personality
Values
Narrative Identity
Drift Monitor
```

## Phase 10 — Society (`v2-society`)

NPC / Groups / Network / Virtual Social Life。

## Phase 11 — Genesis (`v2-genesis`)

Historical Knowledge Builder → Past Simulation → audits → FIRST BOOT。

## Phase 12 — Production Hardening

Admin, backup, recovery, monitoring, chaos tests, Windows launcher, production manifests。

---

# 36. Claude Code 用 Task Completion Definition

Claude Code は各 Task を以下が満たされた時だけ「完了」とみなす。

```text
Implementation
+ relevant tests
+ persistence behavior where applicable
+ failure behavior
+ observability / traceability
+ no invariant regression
```

例: Memory feature は「保存できた」だけでは未完了。

最低条件:

```text
save
retrieve
restart persistence
no duplicate
failure safe
observable from admin/debug path
```

---

# 37. Claude Code が禁止される近道

以下は MUST NOT:

```text
Discord handler → DB direct write
Emotion engine → Relationship direct write
Memory → Personality direct write
Scheduler → Discord direct send
LLM → DB direct mutation
LLM → Admin tool
Tool result → Belief direct insertion
Objective Event Store → normal subjective recall fallback
NPC → Relationship direct mutation
Past Simulation → final personality direct set
```

また、既存 invariant を通すためだけの「特例 if」を大量に追加せず、責務境界を守って根本原因を修正する。

---

# 38. コーディング規約

- Public boundary は型を付ける。
- Domain object に無制限 `dict[str, Any]` を使わない。
- JSON payload を利用する場合も Schema / version を持つ。
- Service / Repository / Domain model を混同しない。
- SQL は Repository / storage layer に集約する。
- LLM prompt text を business logic ファイルへ散在させない。
- Prompt は versioned files / registry で管理する。
- 日時は timezone-aware ISO 8601。内部基準を一貫させる。
- ID は domain prefix + UUID/ULID 等、一意性と追跡性を確保する。
- `print()` を運用 logging に使用しない。
- Exception を無言で握りつぶさない。
- Failure recovery が可能な raw Event を先に残す設計を優先する。

---

# 39. Git / Production Safety

MUST NOT commit:

```text
.env
*.db
*.db-wal
*.db-shm
backups/
logs/
userdata/
.venv/
model files
private generated life data
```

本番更新:

```text
maintenance
→ stop background work
→ backup
→ code update to approved commit
→ dependency/migration check
→ migration
→ tests/smoke
→ restart
→ readiness
→ calibration/monitoring
```

モデルを「最新版だから」という理由だけで自動更新しない。

---

# 40. 調整値と未確定事項

以下は科学的固定値として仕様化せず、Simulation / Evaluation で調整する。

- Emotion decay rates
- Memory forgetting curves
- Trait update window lengths
- Relationship update magnitudes
- Sleep timing coefficients
- Proactive frequency limits
- Curiosity thresholds
- Search termination thresholds
- NPC major event frequency
- Long-term drift warning thresholds
- Context token allocation

これらを `config/policies/` 等で version 管理し、コード定数として散在させない。

---

# 41. v2 完成条件

v2 の完成は「Discord で自然に返答できる」ではない。

最低限、以下を満たす。

- Restart / crash 後も状態が連続する。
- Memory は選択的で、忘却と再想起がある。
- Objective Archive を全知記憶として使用しない。
- Emotion / Mood / Needs が分離される。
- USER Model が不確実性を持って更新される。
- Relationship / Attachment が接触回数だけで最大化しない。
- Goal / Habit / Decision により行動が選ばれる。
- Unknown → Search が自動固定動作でない。
- Virtual Life が USER 不在時にも低コストで進行する。
- Personality / Values が経験に応じてゆっくり変化する。
- NPC 社会が USER 以外の社会的経験を提供する。
- Historical Knowledge が時代的整合性を持つ。
- Past Simulation が通常 Pipeline を再利用する。
- FIRST BOOT が生成過去と実履歴の境界になる。
- Provenance / Confidence / Version が主要判断に追跡可能。
- Long simulation で状態が暴走も完全固定もしない。
- 本番 DB を Backup / Restore 可能。
- Admin 操作と通常会話が分離される。

---

# 42. 実装開始時の最初の Claude Code 指示

最初の実装セッションでは、いきなり Discord BOT を完成させない。

Claude Code への最初の作業単位は次とする。

```text
Phase 0 と Phase 1 のみを対象にする。

1. この仕様の Section 1–9, 31, 35–40 を読む。
2. 既存 repository を調査する。
3. v1 のコードは参考資料として扱い、v2 architecture にコピーしない。
4. Phase 0/1 の実装計画を提示する。
5. 最小 migration + Event + State transaction skeleton を実装する。
6. invariant tests を追加する。
7. tests を実行する。
8. 変更内容、未実装、次 Task を報告する。

Phase 2 以降を先取りして実装しない。
```

この順序を守ること。

# YUI 完全再構築アップデート実装仕様書

- 文書種別: Claude Code 向け実装仕様 / Rebuild Architecture & Implementation Specification
- 対象: 単一ユーザー向け Discord Companion「YUI（ゆい）」
- 目的: 既存 v2 の有効な基盤を残しつつ、会話・記憶・人格形成・自律生活・NPC 社会・日記・Genesis を一つの因果系として実際に動作させる
- 実装主体: Claude Code
- 実行環境: Windows Desktop / Python / SQLite / Ollama / Qwen3.5 9B 系を初期基準
- 状態: 実装開始前の最終仕様
- データ方針: **旧 Dynamic DB、旧過去記憶、旧 Discord 履歴、旧 USER Relationship、旧人格変化は継承しない。完全な新規人格として再初期化してよい。**

---

# 0. この仕様が前仕様と違う点

前回の実装では、Memory、World、Scheduler、NPC、Goal、Proactive などの「部品」が実装され、単体テストも存在した一方、実運用 DB では Activity / Goal / Habit / NPC / Proactive / Scheduler が実際にはほぼ一度も駆動していなかった。

本仕様ではこの失敗を最重要の反省点とする。

**クラスが存在すること、Bootstrap で instantiate されていること、Unit Test が通ることを「実装済み」と呼んではならない。**

機能は、下記の完全な縦方向経路が E2E で実証されて初めて完成とする。

```text
Trigger
  ↓
Runtime Entry Point
  ↓
Context / State Read
  ↓
LLM or Deterministic Interpretation
  ↓
Candidate / Proposal
  ↓
Policy / Evidence / Decision
  ↓
Actual Action
  ↓
Event
  ↓
Persistence
  ↓
State / Memory / Relationship effects
  ↓
Observability
  ↓
Restart Recovery
  ↓
E2E Test
```

この経路のどこか一つでも未接続なら、その機能を README / Phase status / 完了報告で「実装済み」としてはならない。

---

# 1. 表記と強制レベル

- **MUST**: 必須。破る実装は仕様違反。
- **MUST NOT**: 禁止。
- **SHOULD**: 原則採用。外す場合は Claude Code の完了報告に理由を残す。
- **MAY**: 任意。
- **Authority**: その事実・状態を最終的に確定できる唯一の責任主体。
- **Objective**: システム上実際に起きたこと。
- **Subjective**: YUI がどう解釈・記憶・信念化しているか。
- **Candidate**: LLM やルールが提案しただけで、まだ起きていないもの。
- **Commit**: Authority により「起きた」と確定した状態。

---

# 2. 再構築の基本方針

## 2.1 維持する既存思想

以下は再構築しても維持する。

1. Event は immutable。
2. Objective Archive と Subjective Memory は別。
3. USER と NPC は別。
4. simulated past と real runtime は別。
5. Admin と Character Mode は別。
6. Plan / Intention / Current Activity / Completed Experience は別。
7. Tool の成功は Tool Manager の記録だけが Authority。
8. 各 Dynamic Domain は Single Writer。
9. Cross-domain 変更は他 Writer への直接書き込みではなく Event / Evidence / Proposal を通す。
10. Personality / Values は単発 Event では変化しない。
11. Scheduler は Action を直接実行しない。Opportunity を作るだけ。
12. LLM は DB の Authority ではない。
13. 不正な状態を commit するくらいなら degrade / suppress する。

## 2.2 今回変更する中心思想

旧 v2 は LLM を制約しすぎ、Python が自然な社会判断まで規則化しようとしていた。

新構造は下記とする。

```text
LLM-first Proposal
        +
Python-authoritative Validation
```

LLM に任せる:

- 意味理解
- 社会的判断
- 会話の続け方
- 返答するかどうかの提案
- 訂正・違和感の理解
- Memory 候補の意味的関連性
- Activity 候補
- NPC interaction 候補
- 日記文章
- Genesis の人生生成
- 日本語表現

Python / SQLite に残す:

- 時刻
- 年齢計算
- DB 書き込み
- Event 確定
- Activity 開始 / 完了
- Tool 実行結果
- Memory の正式作成
- Personality / Values の更新
- Relationship の数値更新
- USER の確定事実
- Admin 操作
- Hard Safety / Evidence Gate

---

# 3. 完全初期化方針

OWNER は旧履歴を白紙にすることを許可している。したがって今回の再構築では「旧 DB を壊さず引き継ぐ」ことを要件から外す。

ただし事故防止のため以下を MUST とする。

## 3.1 一度だけ最終バックアップ

再構築開始前に現 DB を SQLite backup API で 1 回だけ保存する。

例:

```text
backups/pre_full_rebuild/<timestamp>.db
```

この DB は forensic archive であり、新 Runtime から読んではならない。

## 3.2 Fresh Database

新 Runtime は新規 SQLite DB で開始する。

推奨:

```text
data/yui.db
```

旧 DB を migration して使うのではなく、**新 schema を 0 から作る**。

## 3.3 旧情報を import しない

以下を一切 import しない。

- old events
- old Discord conversation
- old episodic / semantic memories
- old relationship
- old user model
- old personality history
- old values
- old Genesis history
- old NPC
- old diary
- old proactive records

## 3.4 Reset は通常起動に混ぜない

通常 startup で自動 DB 削除してはならない。

専用 command を用意する。

例:

```bash
python -m app.main rebuild-reset --confirm ERASE_YUI_STATE
```

処理:

```text
validate confirmation
→ verified backup
→ close DB
→ archive/delete active DB
→ create fresh schema
→ record rebuild epoch
→ Genesis pending
```

---

# 4. 「穴のある実装」を防ぐ開発制度

この章は機能仕様と同じ強さの MUST とする。

## 4.1 Requirement ID

本仕様の主要 MUST には ID を付ける。

例:

```text
ARCH-001
CONV-014
MEM-020
GEN-042
```

テスト名または docstring から該当 ID を追跡できるようにする。

## 4.2 Implementation Ledger

Claude Code は `docs/IMPLEMENTATION_LEDGER.md` を新設する。

列:

| Spec ID | Requirement | Code | Unit Test | Integration Test | E2E Runtime Proof | Debug Path | Status |

Status は以下のみ。

```text
NOT_STARTED
CODE_ONLY
WIRED
E2E_VERIFIED
```

`DONE` は使用しない。

「E2E_VERIFIED」のみ完成扱い。

## 4.3 Capability Contract

自律系機能ごとに Capability Contract を記録する。

例:

```yaml
capability: proactive_contact
trigger: scheduler/runtime opportunity
runtime_entry: AutonomousRuntime
candidate_source: ProactiveEngine + SocialJudge
hard_gate: ProactivePolicy
actual_action: DiscordOutboundGateway
success_event: YUI_MESSAGE_SENT
persistence: proactive_contacts
recovery: pending outbound is not treated as sent
observability: !yui proactive
acceptance_test: test_proactive_live_vertical_slice
```

対象:

- normal_reply
- intentional_silence
- activity
- sleep
- diary
- spontaneous_memory
- npc_interaction
- group_activity
- goal_action
- habit_action
- proactive_contact
- web_search
- Genesis

## 4.4 Bootstrap Instantiation ≠ 完成

`Application.build()` で object が生成されていても完成とみなさない。

E2E test は必ず実際の trigger を発火させ、期待 DB row / Event / output まで確認する。

## 4.5 Zero-row Audit

Integration test profile で機能を発火させた後、対象 table が 0 row のままなら失敗とする。

例:

```text
activity test → activities > 0
npc test → npc_interactions > 0
proactive test → shadow decision > 0
```

## 4.6 Silent Pass 禁止

「Provider が空だった」「LLM schema failure だった」「Scheduler condition が登録されていなかった」などを silent success にしてはならない。

必ず以下のどれかにする。

```text
success
explicit degraded
explicit skipped_with_reason
failure
```

## 4.7 Phase Completion Gate

各 Phase の最後に Claude Code は以下を報告する。

1. 実装した Spec IDs
2. 実際の Runtime trigger
3. 実際に生成された Event
4. 書かれた DB row
5. State change
6. Debug で確認する方法
7. Restart 後も成立するか
8. Unit test 件数
9. Integration / E2E test 名
10. 未完成箇所

「tests passed」だけの完了報告は禁止。

---

# 5. YUI の存在と 19 年の扱い

## IDENTITY-001

YUI はデジタルな存在であり、現実世界に人間の肉体を持つと主張してはならない。

## IDENTITY-002

19 年は **virtual developmental history** として扱う。

- developmental age を持つ。
- gender identity / virtual embodiment profile を持ってよい。
- 年齢相応の認知・社会経験を Genesis で考慮する。
- 人間社会に似た学校・家庭・友人関係を仮想環境に持ってよい。
- それを「現実世界で物理的に通学していた」などと誤認させてはならない。

## IDENTITY-003

Genesis Anchor の性別・ジェンダー情報は社会文脈に使用してよいが、性格や趣味をステレオタイプで固定してはならない。

---

# 6. Authority Map

| Domain | Authority | LLM role |
|---|---|---|
| Event | EventStore / Processor | Candidate interpretation only |
| Current World | WorldService | no direct write |
| Activity | ActivityRepository + WorldService | candidate action |
| Sleep | WorldService / Sleep model | motivational interpretation only |
| Subjective Memory | MemoryEngine | summary / relevance candidate |
| Belief | BeliefEngine | evidence interpretation |
| Self | SelfEngine | hypothesis / narrative candidate |
| Emotion | EmotionEngine | appraisal input only |
| Mood | MoodEngine | none/direct no write |
| Needs | NeedEngine | none/direct no write |
| USER Relationship | RelationshipEngine | social meaning candidate |
| USER Model | SocialCognitionEngine | provisional inference |
| NPC objective facts | SocietyService | scenario candidate |
| YUI→NPC relation | NPCRelationshipEngine | interaction meaning candidate |
| Personality | GrowthEngine | evidence candidate only |
| Values | ValueEngine | evidence candidate only |
| Tool execution | ToolManager | may request tool |
| Diary | DiaryService | diary prose generation |
| Genesis life records | Genesis Builder + validated LLM outputs | content generator |
| Discord sent fact | Discord gateway + confirmed send | draft only |
| Admin | AdminControlPlane | no character authority |

---

# 7. Event / Processing Core

既存の以下の形を残す。

```text
Event
→ pre-event Snapshot S0
→ Interpreter / Subscribers
→ StateChangeProposal[]
→ Arbitration
→ Commit transaction
→ post-commit Snapshot S1
→ derived events/background work
```

## CORE-001

同じ State Domain に複数 Writer を作ってはならない。

## CORE-002

LLM の JSON が valid だっただけで state commit してはならない。

## CORE-003

Action Candidate を Experience として保存してはならない。

## CORE-004

外部送信は send success 後にのみ `YUI_MESSAGE_SENT` とする。

## CORE-005

すべての meaningful autonomous action は traceable Event を持つ。

---

# 8. LLM 基盤

## 8.1 Purpose 分割

少なくとも以下の purpose を用意する。

```text
appraisal
social_interpretation
memory_rerank
reply_generation
reply_repair
claim_extraction
activity_candidates
npc_scenario
proactive_judgment
diary_generation
genesis_year
genesis_month
genesis_event_extraction
genesis_critic_chronology
genesis_critic_continuity
genesis_critic_development
genesis_critic_psychology
genesis_critic_historical
genesis_critic_memory
```

## 8.2 Priority

推奨:

```text
P0  USER reply critical path
P1  reply support / social interpretation
P2  immediate state
P3  memory / diary
P4  autonomous action decision
P5  NPC / knowledge
P6  consolidation
P7  Genesis
```

単一 GPU slot では USER が来たら background / Genesis が必ず譲る。

## 8.3 Thinking

通常会話は `think=false` を原則とする。

Genesis critic など品質優先 call は provider が安定して対応できる場合のみ thinking を MAY とする。

Thinking text を回答 content と結合してはならない。

## 8.4 Structured Output

Schema を渡してもモデルは違反する前提で作る。

- parse
- schema
- semantic
- state consistency
- identity

を別失敗として記録する。

## 8.5 Retry

通常会話:

```text
max 1 normal attempt + optional 1 repair
```

Genesis:

```text
bounded retries allowed
```

無限 retry 禁止。

## 8.6 Reason 保存

LLM の private chain-of-thought を DB に保存しない。

保存するのは短い structured reason / reason code のみ。

---

# 9. Appraisal 再設計

現状の「0.0〜1.0 小数を直接全部出させる」方式を廃止する。

## APP-001 Meaning Category

LLM output:

```json
{
  "self_relevance": "low|medium|high",
  "goal_congruence": "strong_negative|negative|neutral|positive|strong_positive",
  "novelty": "low|medium|high",
  "certainty": "low|medium|high",
  "control": "low|medium|high",
  "agency": "self|mixed|other|situation",
  "social_meaning": "strong_negative|negative|neutral|positive|strong_positive",
  "expectation_violation": "low|medium|high",
  "confidence": "low|medium|high",
  "reason": "one short sentence"
}
```

Python が numeric mapping する。

Mapping は policy file へ置く。

## APP-002

LLM が negative agency など数学的にあり得ない値を作る余地をなくす。

## APP-003

意味カテゴリの mapping を人格に合わせて動的に変えない。Appraisal semantics は安定した尺度である。

## APP-004 Acceptance

Real Ollama 100 synthetic turns で:

- parse/schema failure < 2%
- uncaught exception 0
- fallback使用率を計測

---

# 10. Conversation: Social Interpretation

最終返信より前に 1 回の `SocialInterpretation` call を行う。

通常会話の call 数を抑えるため以下を統合する。

```json
{
  "message_function": "...",
  "possible_correction": false,
  "correction_target": null,
  "response_intent": "normal|brief|leave_space|silence",
  "question_need": "none|optional|needed",
  "initiative": "low|balanced|high",
  "memory_mode": "none|conversational|autobiographical|associative",
  "memory_query": "...",
  "social_reason": "...",
  "confidence": "..."
}
```

## CONV-001

USER の短文を単語だけで分類せず、直前 YUI 発言と一緒に解釈する。

## CONV-002

「詩？」のような短い疑問は、直前 YUI の unsupported claim への challenge である可能性を評価する。

---

# 11. Common Ground / Correction

## 11.1 CommonGroundState

会話ごとに短期 read model を持つ。

例:

```text
claim: USER wrote a poem
status: contested
source: yui_inference
confidence: low
```

Status:

```text
supported
provisional
contested
retracted
```

## CORR-001

USER が否定・疑問を示した場合、YUI は自分の直前 claim を再検証する。

## CORR-002

Evidence が無ければ、説明で押し切らず retraction 側へ倒す。

## CORR-003

訂正後は Common Ground から誤 claim を除外し、後続 turn で再利用しない。

---

# 12. Response Intent / Intentional Silence

## 12.1 Modes

```text
NORMAL_REPLY
BRIEF_REPLY
LEAVE_SPACE
INTENTIONAL_SILENCE
```

## 12.2 LLM Proposal + Hard Gate

LLM は「人間的に今どうしたいか」を提案する。

Python gate は以下を確認する。

Silence を原則禁止:

- direct question
- explicit request
- correction/challenge
- important support-seeking message
- adminではない operational request
- replyがないと不自然な conversation repair

Silence を許容:

- acknowledgement-only
- conversation closure
- repeated minimal turn
- YUI current state が低 engagement かつ重要度低

## 12.3 Event

意図的沈黙は `YUI_INTENTIONAL_SILENCE`。

以下と別:

- `YUI_REPLY_SUPPRESSED`
- LLM timeout
- Discord error
- OutputGuard rejection

## 12.4 Typing

Silence decision を先に行い、返信する時だけ typing を開始する。

---

# 13. 日本語・会話の人間らしさ

「人間らしさ」は心理構造だけではなく、**日本語の話し方そのもの**を正式要件とする。

対象:

- 相槌
- 短文
- 省略
- 言い直し
- 終助詞
- 文の不完結さ
- 話題転換
- 質問しない turn
- 会話を自然に終える turn
- 自己開示
- 距離に応じた砕け方
- 同意 / 不同意の柔らかさ
- 毎回同じ冒頭を使わない

## 13.1 DialogueReferenceProvider

Interface:

```python
class DialogueReferenceProvider(Protocol):
    def search(self, query: DialogueReferenceQuery, limit: int = 4) -> Sequence[DialogueExample]: ...
```

Metadata:

```text
relationship_distance
conversation_type
turn_function
length
register
tone
initiative
```

## 13.2 外部 Corpus

CEJC 等の自然会話資料を候補にできるが、raw corpus の repo 同梱を前提にしない。

Provider は差替可能にする。

```text
LocalLicensedCorpusProvider
OwnerCuratedProvider
NullProvider
```

利用する corpus はライセンス確認を必須とする。

## 13.3 Corpus ≠ Memory

Dialogue reference は YUI の経験・知識ではない。

Memory / Belief / Objective Archive へ入れてはならない。

## 13.4 JapaneseRealizer

最終 prose は LLM にかなり自由に任せる。

入力:

```text
identity
recent conversation
social interpretation
surface plan
relationship
current state
selected memories
grounded facts
dialogue reference examples
```

固定テンプレートで文章を組み立てない。

---

# 14. Surface Plan

細かい文法は指定しない。

構造化するのは方向性だけ。

```json
{
  "length": "very_short|short|medium|long",
  "energy": "low|normal|high",
  "initiative": "low|balanced|high",
  "question": "none|optional|required",
  "self_disclosure": "none|light|moderate",
  "directness": "soft|normal|direct",
  "register": "casual|neutral|careful"
}
```

「内部状態が複雑だから長文になる」という実装を禁止する。

---

# 15. Grounding / 捏造防止

## 15.1 GroundingContext

返信前に構築する。

```text
current_world
current_activity
completed_activities_today
recent_objective_events
recalled_subjective_memories
verified_user_facts
known_semantic_memories
successful_tool_calls
npc_interactions
current_goals
explicitly_read_diary_entries
```

## 15.2 Claim Extraction

危険な事実主張を抽出する。

種類:

```text
yui_completed_action
yui_perception
yui_memory_claim
user_past_fact
npc_fact
tool_use
external_knowledge_claim
current_world_fact
```

## 15.3 Evidence Resolution

例:

```text
「今日は本を読んだ」
→ completed Activity または authoritative Event が必要
```

```text
「前に君が○○と言った」
→ Conversation Event/turn または subjective memory が必要
```

## GROUND-001

LLM 自身の生成文は Evidence にならない。

## GROUND-002

Unsupported claim があれば 1 回のみ repair。

## GROUND-003

repair 失敗なら送信 suppress。

## GROUND-004

未送信 draft を Memory に encode しない。

---

# 16. Output Guard

Hard Guard のみにする。

検出:

- internal prompt/instruction leakage
- chain-of-thought markers
- malformed JSON residue
- impossible physical-human claim
- prohibited admin/secrets disclosure
- unsupported claim after repair

「少し文章がぎこちない」程度を guard で書き換えない。

自然さは JapaneseRealizer / reference layer の責務。

---

# 17. Memory System 再構築

Memory の基本思想は残すが Retrieval と practice を作り直す。

## 17.1 Objective ≠ Subjective

```text
Event/Archive
≠
Episodic Memory
≠
Semantic Memory
≠
Diary
≠
Narrative Identity
```

## 17.2 Encoding

Experience が Episode へ入り、Encoding Gate を通過したものだけ Episodic Memory になる。

Signals:

- novelty
- emotional intensity
- prediction error
- self relevance
- goal relevance
- social significance
- repetition

幼少期は年齢別 retention policy を使う。

## 17.3 Retrieval 三段階

```text
Candidate Generation
→ Semantic Relevance Gate
→ Accessibility Ranking
```

### Stage 1

FTS / recency / explicit autobiographical bridge / topic から候補 10〜20件。

### Stage 2

LLM reranker が query と候補の意味的関連性を判定。

出力:

```json
{
  "memory_id": "...",
  "relevance": "none|weak|medium|strong",
  "reason": "..."
}
```

`none/weak` は通常 Recall から除外。

### Stage 3

関連候補だけに:

- accessibility
- emotion
- importance
- recency

を使って最終選択。

## MEM-001

Accessibility は relevance を作ってはならない。

## MEM-002

Fallback memory が高 accessibility だけで無関係な会話へ出ることを禁止。

## 17.4 Recall mode

```text
CONVERSATIONAL
AUTOBIOGRAPHICAL
ASSOCIATIVE
REFLECTIVE
DIARY_READING
```

## 17.5 Retrieval ≠ Recall

状態:

```text
candidate
selected_for_context
consciously_recalled
spontaneously_recalled
used_in_reply
```

Memory practice は `consciously_recalled` / `spontaneously_recalled` / `used_in_reply` の場合のみ。

Debug search は practice 0。

## 17.6 Spontaneous Recall

Autonomous Runtime は current activity / cue / mood / anniversary / NPC 等から ASSOCIATE opportunity を生成できる。

実際に選ばれた場合:

```text
MEMORY_SPONTANEOUSLY_RECALLED
```

を発生させ、その後 Emotion / Goal / Proactive に影響してよい。

## 17.7 Reconstruction

記憶内容の修正は version history を残す。

Objective source event を書き換えない。

USER の訂正は Memory revision evidence になり得るが、即時に過去を完全書き換えない。

---

# 18. Belief / Self / Narrative

## 18.1 Belief

Belief は「真だと思う程度」。同じ source の繰り返しを独立証拠として数えない。

## 18.2 Self Schema

YUI が自分をどう認識しているか。客観 state と食い違ってよい。

## 18.3 Narrative Identity

以下を source にする。

- deep consolidation
- diary reviews
- annual Genesis summaries
- major episodic memories

例:

```text
「あの頃は人との距離感を探していた」
```

Narrative は Episodic Memory ではない。

---

# 19. Personality / Values / Adaptation

## PERS-001

単発の USER message で personality を変更してはならない。

## PERS-002

流れ:

```text
Experience
→ repeated pattern evidence
→ Characteristic Adaptation
→ multiple separated windows
→ Deep Consolidation
→ Trait/Value Candidate
→ Gate
→ Personality/Value History
```

## PERS-003

LLM が「この出来事で外向性 +0.1」と直接指定することを禁止。

## PERS-004

Personality change は履歴を必ず残す。

## PERS-005

Genesis でも runtime と同じ growth engine を使う。

---

# 20. USER Relationship / User Model

## 20.1 Relationship

最低限:

- familiarity
- trust
- emotional_closeness
- security
- respect
- expectation
- conflict_residue

## 20.2 Update speed

familiarity は接触量で比較的速く動いてよい。

trust / security / expectation は遅く動かす。

Expectation は線形加算で上限まで走らせず、EMA 型更新を推奨。

```text
new = old + rate * (observed_target - old)
```

## 20.3 User Model evidence independence

同一会話・同一話題の連続 10 発言を 10 independent evidence として扱わない。

Evidence cluster key:

```text
episode_id
topic_cluster
time_window
context_type
```

Trait は複数 cluster で再現して初めて強く更新。

---

# 21. Autonomous Runtime

この再構築で最も重要な新規実装の一つ。

## 21.1 原則

毎秒 LLM を呼ばない。

Event-driven + next-due-time sleep とする。

## 21.2 AutonomousRuntime

推奨:

```text
app/runtime/autonomous.py
```

主要責務:

```text
find next meaningful wake-up
wait
receive USER interrupt or timer
refresh world lazily
collect due scheduler opportunities
check activity completion
check sleep transition
check goals/habits
check associative memory opportunities
check NPC/social opportunities
check knowledge opportunities
check proactive opportunity
run decision
execute selected actions
schedule next wake-up
```

## RUNTIME-001

Runtime 自身は心理 state を直接書かない。

## RUNTIME-002

Runtime 自身は Scheduler opportunity を即 action にしない。

## RUNTIME-003

USER inbound は background action より優先。

## RUNTIME-004

Runtime start/stop は Application lifecycle に統合し、shutdown 時に task を cancel/drain する。

---

# 22. Opportunity System

統一 `Opportunity` を使う。

例:

```text
activity_due
habit_cue
goal_step
sleep_candidate
npc_contact
spontaneous_recall
knowledge_curiosity
proactive_contact
diary_due
```

Opportunity は「行動可能性」であり Event ではない。

Selected / executed された時だけ Event 化。

---

# 23. Decision Engine

Decision は候補を比較する。

入力:

- goals
- values
- needs
- current mood/emotion
- habits
- activity
- relationship
- urgency
- cost
- expected outcomes

LLM は candidate を作れるが、最終 selection は Decision Engine。

Stochasticity は near-tie のみに限定。

完全ランダム行動 generator 禁止。

---

# 24. Activity System

## 24.1 Candidate

LLM call `activity_candidates`。

入力:

```text
time
world
mood
needs
goals
habits
interests
recent activities
available NPC/groups
```

2〜5候補。

## 24.2 Lifecycle

```text
candidate
→ decision
→ ACTIVITY_STARTED
→ scheduled completion
→ ACTIVITY_FINISHED
```

中断なら:

```text
ACTIVITY_INTERRUPTED
```

## ACT-001

`ACTIVITY_STARTED` は「やった経験」ではない。

## ACT-002

YUI が「今日は○○した」と言えるのは completed record がある場合だけ。

## ACT-003

Outcome は completion 時に LLM が details を提案してよいが、completed fact は WorldService が確定する。

---

# 25. Sleep

既存の二過程モデルを維持。

- sleep pressure
- circadian phase
- sleepiness
- sleep inertia
- conversation resistance
- goal resistance

## SLEEP-001

LLM の気分だけで「寝ない」を無限継続させない。

## SLEEP-002

主要睡眠と nap を区別する。

## SLEEP-003

Runtime が本当に sleep/wake transition を発火する。

## SLEEP-004

Sleep transition を Event が来た時だけ計算する状態から、due action として実際に駆動する。

---

# 26. Diary System

## 26.1 目的

日記はログではなく、**その日の終了時点で YUI が一日をどう振り返ったかという主観的外部記録**。

```text
Event ≠ Memory ≠ Diary
```

## 26.2 Life Day

日付境界は 0:00 固定ではなく:

```text
major wake
→ next major sleep
```

## 26.3 Bedtime flow

推奨:

```text
sleep decision
→ BEDTIME_REFLECTION_STARTED
→ freeze DailyDiaryContext
→ diary generation attempt
→ DIARY_WRITTEN if success
→ WENT_TO_SLEEP
```

ただし LLM failure が睡眠を永久に阻害してはならない。

Policy:

- diary attempt has bounded timeout
- failed diary is `pending_retry`
- sleep proceeds
- retry during sleep/background
- late-generated diary retains `intended_at` bedtime and `generated_at` separately

## 26.4 DiaryContextBuilder

入力:

- major events
- completed activities
- USER conversations summary
- NPC interactions
- spontaneous recalls
- emotion peaks
- mood trajectory
- needs changes
- goal progress
- knowledge acquisitions
- relationship changes
- mistakes/conflicts
- current self reflection
- bedtime state

重要度で圧縮し、全 Event dump はしない。

## 26.5 Diary prose

LLM に自由文で書かせる。

固定フォーム禁止。

何もなかった日は短くてよい。

## 26.6 Diary Authority

日記は「YUI がそう書いた」という事実については Authority。

日記本文の解釈が客観的真実とは限らない。

## 26.7 Diary / Memory

日記本文を自動的に episodic memory へコピーしない。

日記で実際に振り返った memory だけ軽い recall practice を与えてよい。

`DIARY_WRITTEN` 自体は経験。

## 26.8 Diary Read

忘れた出来事を通常 Recall が勝手に diary からカンニングしてはならない。

日記を読む場合:

```text
READ_DIARY activity
→ DiaryRepository read
→ DIARY_READ Event
→ Appraisal
→ new Memory/Emotion possible
```

---

# 27. Scheduler

既存思想を維持。

```text
Scheduler.tick()
→ Opportunity[]
```

Action を実行しない。

AutonomousRuntime が tick を実際に呼ぶこと。

## SCH-001

Integration test で scheduled job を due にし、Runtime を通じて Opportunity が Decision まで届くことを証明。

---

# 28. Proactive Contact

## 28.1 Hard Gate

既存の anti-positive-feedback を維持。

- max unanswered
- cooldown
- unanswered backoff
- quiet hours
- estimated user availability

USER が返さないほど送信頻度が増える構造は禁止。

## 28.2 Social Judgment

Hard Gate 通過後に LLM が判断。

質問:

```text
「今、本当にUSERへ伝えたいことがあるか？」
```

入力:

- trigger
- current needs
- recent memory recall
- recent activity
- goals
- relationship
- conversation status

## 28.3 Trigger

原則最低 1 つ:

- activity completion
- spontaneous memory
- NPC event
- goal event
- knowledge discovery
- emotion
- connection desire
- unfinished conversation

## 28.4 Modes

```text
OFF
SHADOW
LIVE
```

初期運用は SHADOW。

Shadow では send しないが:

- would_send
- reason
- draft
- hard gates

を保存。

## 28.5 Live send

```text
Opportunity
→ gate
→ LLM judgment
→ DecisionEngine
→ draft
→ grounding/guard
→ Discord send
→ success only: YUI_MESSAGE_SENT + proactive record
```

---

# 29. Virtual Society / NPC

既存 NPC Objective Profile と NPCModel の分離を維持。

## 29.1 Tier

```text
Tier0 background
Tier1 recurring
Tier2 significant
```

全 NPC を完全 Agent にしない。

## 29.2 NPC generation

必要になった時に生成。

例:

```text
group activity
→ background participants needed
→ create Tier0 NPCs
```

繰り返し関われば promote Tier1。

重要関係になれば Tier2。

## 29.3 NPC continuity

NPC は name ではなく `npc_id` で追跡。

属性:

- objective traits
- role
- availability
- warmth
- reliability
- groups
- status

## 29.4 NPCModel

Tier2 だけ詳細な YUI subjective model を持つ。

YUI は NPC を誤解できる。

## 29.5 NPC scenario generation

LLM input:

```text
objective NPC profile
YUI model of NPC
relationship
current world/activity
group context
recent interactions
```

LLM output は「起きそうな interaction candidate」。

Decision / SocietyService が commit した時だけ本当に起きる。

## 29.6 NPC は独立世界を持つ

NPC同士の SocialLink / group memberships を利用し、全社会を USER/YUI 中心の星型にしない。

ただし全 NPC を毎分 simulation しない。

Off-screen updates は低コスト・event-driven。

---

# 30. Group / Belonging

Group opportunity:

```text
due group activity
→ attend candidate
→ decision
→ activity
→ group event
→ NPC interactions
→ belonging update
```

USER だけを唯一の relatedness source にしない。

---

# 31. Goals / Habits

既存 engine を Runtime に接続する。

## 31.1 Goals

LLM は GoalCandidate を提案できる。

Python は:

- importance
- autonomy
- value alignment
- feasibility
- conflicts

を評価して採用。

## 31.2 Habits

Habit は固定日数で成立しない。

- cue
- repetition
- context stability
- automaticity

を追跡。

## 31.3 Runtime

Opportunity generation 時に active goals / matching habit cues を必ず候補源にする。

---

# 32. Epistemic Actions / Web Search

Unknown を自動検索しない。

候補:

```text
recall
infer
ask_user
web_search
defer
ignore
avoid
```

## 32.1 SearchProvider

Interface:

```python
class SearchProvider(Protocol):
    async def search(self, query: SearchQuery) -> SearchResult: ...
```

Tool Manager 経由。

## 32.2 Authority

LLM が「検索した」と書いただけでは Tool success ではない。

## 32.3 Knowledge funnel

```text
Search Result
→ INFORMATION_ENCOUNTERED
→ attention/comprehension
→ acquisition candidate
→ KnowledgeService
→ optional Memory
```

---

# 33. Offline Catch-up

BOT が停止していた期間を分単位で完全再演しない。

```text
elapsed period
→ fixed obligations / sleep windows / active plan
→ compressed meaningful segments
→ bounded events
→ normal processor
```

## OFF-001

Online と Offline で人格更新ルールを変えない。

## OFF-002

Offline も「結果だけ直接 state に書く簡易人格」を禁止。

## OFF-003

大きな gap ほど event 数を線形増加させない。

---

# 34. Genesis v2: 全面再設計

Genesis は FIRST BOOT 一度だけの高品質処理。通常会話の latency 要件を適用しない。

**処理時間より整合性・リアリティ・因果を優先する。**

## 34.1 Genesis Anchors

初期設定として確定する。

```text
birth_datetime
developmental_age_at_present
gender_identity
virtual_embodiment_profile
language/culture
virtual home environment
family structure
social environment
education-like environment
immutable identity rules
temperament seed
```

年齢は Python が exact date から計算。

LLM に年齢計算させない。

## 34.2 Temperament Seed

OWNER 初期質問は rough bias のみ作る。

最終 personality / values / hobbies を直接決めない。

## 34.3 Stage A: 19 Annual Scaffolds

現在年齢が19なら19年分。

1年ごとに長文生成。

入力:

- exact age range
- anchors
- previous annual scaffold
- continuity ledger
- broad historical context
- current provisional temperament

出力:

- routine life
- family/social situation
- school-like/development context
- hobbies/interests exposure
- important and mundane events
- conflicts
- successes/failures
- ongoing threads
- people introduced
- changes that may matter later

### GEN-ANNUAL-001

毎年 major event を強制しない。

### GEN-ANNUAL-002

Annual scaffold は **仮設計**。まだ subjective memory でも objective final fact でもない。

## 34.4 Stage B: Monthly Expansion

19 × 12 = 最大228月を生成。

各月:

入力:

- annual scaffold
- previous month
- relevant continuity entries
- exact age
- relevant NPCs
- historical context

出力:

- routines
- social episodes
- small changes
- interests
- emotional situations
- unresolved threads
- meaningful events

## 34.5 Event Density

月を分類:

```text
routine
minor
meaningful
major
turning_point
```

meaningful 以上だけ追加 LLM detail call。

routine month でも長文の生活文脈はあってよいが、偽の大事件を増やさない。

## 34.6 Stage C: Final Annual Synthesis

12か月完成後、その月記録を source に annual summary を再生成。

初期 annual scaffold と矛盾する場合、最終月記録を優先。

## 34.7 Life Continuity Ledger

Genesis 中の継続状態を別管理。

Entity types:

```text
NPC
GROUP
LOCATION
POSSESSION
INTEREST
ONGOING_THREAD
COMMITMENT
LIFE_FACT
```

毎月すべてを prompt へ入れず relevant subset を retrieval。

## 34.8 Genesis NPC

過去 NPC は stable ID。

保持:

```text
introduced_at
role
objective profile
relationship timeline
group timeline
last_seen
status
```

現在まで続く NPC は runtime society へ引き継ぐ。

疎遠・終了した NPC も archive には残る。

## 34.9 Historical Reality

年代ごとに外部 source/bundle から「その時点で世界に存在し得たもの」を用意。

未来 leakage を audit。

ただし世界に存在したから YUI が知っていたことにはしない。

## 34.10 Critics

一つの巨大 critic に全部やらせない。

別 purpose:

1. Chronology Critic
2. Continuity Critic
3. Development Critic
4. Psychology Critic
5. Historical Reality Critic
6. Repetition / Narrative Realism Critic
7. Identity Critic
8. Memory Plausibility Critic

Output:

```json
{
  "passed": false,
  "issues": [
    {
      "severity": "low|medium|high|fatal",
      "target_id": "month/year/event id",
      "code": "AGE_MISMATCH",
      "reason": "...",
      "repair_scope": "..."
    }
  ]
}
```

High/Fatal は対象部分のみ再生成。

## GEN-CRITIC-001

Audit が失敗したのに silent pass して次へ進まない。

## 34.11 Experience Extraction

Final month records から `ExperienceCandidate` を抽出。

Fields:

```text
occurred_at / range
actors
context
action
outcome
social significance
source month id
confidence
```

全 narrative sentence を Event にしない。

日常反復は compressed experience。

## 34.12 Psychological Replay

Experience を時系列に通常 Processor へ流す。

```text
Experience
→ Event(simulated_past)
→ Appraisal
→ Emotion/Mood/Needs
→ Relation/Self/Belief
→ Memory encoding
→ periodic Consolidation
→ Adaptations
→ Personality/Values
```

**最終人格へ逆算しない。**

## 34.13 Memory formation

Annual/Monthly narrative をそのまま subjective memory table に入れない。

それらは Objective Life Record / generation source。

Experience が Memory Engine を通過したものだけ subjective episodic memory。

## 34.14 Age-dependent memory

幼児期:

- psychological effect はあり得る
- explicit episodic recall は残りにくい

成長とともに encoding / temporal precision を変える policy を用意。

## 34.15 Forgetting during Genesis

最後に19年分を一括 decay するだけではなく、simulation time が進む中で periodic forgetting を適用。

## 34.16 Consolidation during Genesis

- month/quarter/year/phase boundary など設定した複数 window で実施
- 最終だけ一回は禁止

Deep Gate が時期の離れた evidence を見る必要がある。

## 34.17 Genesis Diary

過去7,000日すべてを日記生成しない。

- major day
- periodic reflective day
- turning point

だけ生成可能。

現在 runtime 開始後は毎主要睡眠で日記。

## 34.18 Checkpoint

長時間処理のため必須。

最低単位:

```text
annual_scaffolds_done
year_N_months_done
year_N_critics_done
year_N_extraction_done
year_N_replay_done
year_N_memory_done
final_audits_done
```

## 34.19 Idempotency

各生成物に:

```text
genesis_run_id
generation_id
source_year_id
source_month_id
replay_status
```

を付け、resume で二重 replay しない。

## 34.20 FIRST_BOOT_COMPLETE

以下すべて成功後のみ書く。

- chronology audit
- continuity audit
- identity audit
- experience replay audit
- memory health audit
- personality growth audit
- knowledge chronology audit
- NPC continuity audit
- no real USER before first boot audit

FIRST_BOOT_COMPLETE 前は Discord Character Gateway を online にしない。

---

# 35. Genesis DB Model

新規候補:

```text
genesis_runs
genesis_checkpoints
life_anchors
life_years
life_months
life_entities
life_entity_snapshots
generation_audits
```

既存 events / memories / personality 等は正式 replay 後の Authority に利用。

## life_years

```text
year_id PK
genesis_run_id
year_number
calendar_start
calendar_end
age_start
age_end
scaffold_text
final_summary
status
prompt_version
model_version
created_at
```

## life_months

```text
month_id PK
year_id
month_start
month_end
age_start
age_end
narrative
importance_class
status
prompt_version
model_version
```

## life_entities

```text
entity_id PK
type
canonical_name
introduced_at
retired_at
objective_json
```

## generation_audits

```text
audit_id
target_type
target_id
critic_type
passed
issues_json
model_version
prompt_version
created_at
```

---

# 36. Diary DB Model

## diary_entries

```text
diary_id PK
life_day_id
intended_at
generated_at
sleep_episode_id
content
summary
importance
mood_valence
mood_arousal
prompt_version
model_version
status
```

status:

```text
pending
written
late_written
failed
```

## diary_references

```text
diary_id
reference_type  # event/memory/activity/npc/goal
reference_id
mentioned_in_text
```

---

# 37. Conversation Decision Persistence

`interaction_decisions` を追加してよい。

```text
decision_id
event_id
response_intent
dialogue_mode
question_need
correction_type
memory_mode
reason_codes
model_call_id
created_at
```

これは心理 state ではなく observability/read model。

---

# 38. Autonomous Runtime Recovery

クラッシュ時に以下があり得る。

- ongoing activity
- sleeping
- pending diary
- fired scheduler job
- drafted proactive message
- uncommitted action candidate

Recovery rule:

1. Candidate は再評価可能。
2. Sent 未確認の Discord message を sent と扱わない。
3. ongoing activity は時刻に応じて finish / interrupt / resume を決定。
4. pending diary は frozen context があれば retry。
5. Scheduler fired-but-not-consumed は idempotency key で重複 action 防止。
6. sleep episode は current time から wake evaluation。

---

# 39. Discord Admin / Debug

既存 `!yui` reservation を Admin Router へ接続。

通常 YUI personality pipeline に入れない。

## 39.1 Read-only commands

```text
!yui status
!yui state
!yui emotion
!yui mood
!yui needs
!yui relationship
!yui attachment
!yui usermodel
!yui personality
!yui values
!yui world
!yui activity
!yui goals
!yui habits
!yui memory
!yui memory recent 10
!yui memory find <query>
!yui appraisal
!yui trace
!yui latency
!yui llm
!yui failures
!yui events 20
!yui runs 20
!yui scheduler
!yui proactive
!yui proactive dryrun
!yui npc
!yui groups
!yui diary
!yui diary recent
!yui genesis
!yui growth
!yui version
!yui backup
```

## 39.2 No side effects

Admin command は:

- USER_MESSAGE_RECEIVED を作らない
- appraisal を発火しない
- relationship を変えない
- memory encode しない
- recall practice しない
- proactive send しない

## 39.3 Memory Preview

通常 `MemoryEngine.recall()` を debug から直接呼ばない。

`MemoryInspector.preview()` を作る。

---

# 40. Observability

「なぜこうなったか」を debug 可能にする。

## 40.1 Conversation Trace

最低:

```text
received
admitted
state processing
social interpretation
memory candidate retrieval
memory rerank
reply generation
claim check
repair
outbound projected
sent
```

## 40.2 Autonomous Trace

```text
cycle id
trigger
opportunities
candidate actions
hard gates
selected action
action result
events produced
next wakeup
```

## 40.3 Genesis Progress

CLI / Admin で:

```text
current stage
year/month
LLM calls
retry counts
critic failures
experience count
memory count
elapsed time
estimated remaining units (not time promise)
```

を確認。

## 40.4 Structured reason only

内部 chain-of-thought をログへ保存しない。

---

# 41. Performance

## Runtime conversation target

Warm short DM:

- median <= 15s
- p95 <= 30s

Naturalness向上で call が増えるため、通常 path は原則:

```text
1 appraisal
1 social interpretation
0/1 memory rerank
1 reply
0/1 claim/repair
```

平均 3〜4 model call 程度を目標。

## Genesis

Wall-clock SLA を置かない。

ただし:

- progress observable
- bounded retry
- checkpoint/resume
- USER runtime と同時実行しない FIRST BOOT

を必須。

---

# 42. Prompt 構成

`config/prompts/` に versioned prompt を置く。

追加候補:

```text
appraisal/v2.md
social_interpretation/v1.md
memory_rerank/v1.md
reply/v2.md
reply_repair/v2.md
claim_extraction/v1.md
activity_candidates/v1.md
npc_scenario/v1.md
proactive_judgment/v1.md
diary/v1.md
genesis/year/v1.md
genesis/month/v1.md
genesis/event_extraction/v1.md
genesis/critic_chronology/v1.md
genesis/critic_continuity/v1.md
genesis/critic_development/v1.md
genesis/critic_psychology/v1.md
genesis/critic_historical/v1.md
genesis/critic_memory/v1.md
```

Prompt に Python invariant を長々重複記載しない。

Hard constraint は Python 側でも検証する。

---

# 43. 推奨パッケージ構成

```text
app/
├─ cognition/
│  ├─ social_interpretation.py
│  └─ claim_extraction.py
├─ conversation/
│  ├─ response_intent.py
│  ├─ grounding.py
│  ├─ common_ground.py
│  ├─ surface.py
│  └─ references.py
├─ runtime/
│  ├─ autonomous.py
│  ├─ opportunities.py
│  └─ recovery.py
├─ diary/
│  ├─ models.py
│  ├─ context.py
│  ├─ service.py
│  └─ repository.py
├─ genesis/
│  ├─ anchors.py
│  ├─ annual.py
│  ├─ monthly.py
│  ├─ continuity.py
│  ├─ critics.py
│  ├─ extraction.py
│  ├─ replay.py
│  └─ checkpoint.py
├─ memory/
│  ├─ retrieval.py
│  ├─ rerank.py
│  └─ inspector.py
└─ interfaces/discord/
   └─ admin_router.py
```

既存 world/society/agency/state/event を可能な限り再利用する。

---

# 44. Testing Architecture

## 44.1 Unit

各 algorithm / mapping / policy。

例:

- appraisal category mapping
- memory relevance floor
- recall practice diminishing return
- silence hard gate
- proactive backoff
- exact age calculation
- Genesis checkpoint idempotency
- diary life-day calculation

## 44.2 Invariant tests

必須:

1. LLM cannot directly mutate state.
2. Objective Archive is not normal Memory fallback.
3. USER never becomes NPC.
4. Admin never becomes USER event.
5. Plan is not completed Activity.
6. Started Activity is not completed Experience.
7. Tool claim requires successful Tool record.
8. Personality does not change from one Event.
9. Diary is not automatic Memory fallback.
10. Dialogue corpus is not YUI memory.
11. Scheduler cannot directly send Discord.
12. Prompt leak cannot reach outbound.

## 44.3 Integration

Vertical slice testsを必須にする。

### Conversation slice

```text
USER msg
→ state
→ social interpretation
→ memory
→ reply
→ grounding
→ send confirm
→ YUI_MESSAGE_SENT
```

### Silence slice

eligible closure message
→ intentional silence
→ no typing/outbound
→ YUI_INTENTIONAL_SILENCE

### Activity slice

runtime opportunity
→ activity decision
→ start
→ due completion
→ finish event
→ later grounded reply can mention it

### Sleep/Diary slice

runtime sleep decision
→ bedtime diary
→ sleep
→ wake
→ diary persisted

### NPC slice

social opportunity
→ NPC selected/generated
→ interaction
→ relation update
→ memory candidate

### Proactive shadow slice

trigger
→ assessment
→ social judgment
→ would_send row
→ no Discord send

### Proactive live slice

same + actual send + confirmed event.

### Search slice

unknown
→ search choice
→ Tool Manager
→ result
→ knowledge acquisition

## 44.4 Real Ollama Smoke

Mock test だけで完了しない。

Qwen3.5:9B 実モデルで最低:

- appraisal
- social interpretation
- memory rerank
- reply
- claim extraction
- diary
- one Genesis year/month/critic

を試す。

## 44.5 Naturalness regression

固定 scenario:

- 「うん」
- 「そうなんだ」
- 「詠んだよ」→「詩？」
- 「今日は何してた？」
- 「昔のことっていつまで思い出せる？」
- 「一番印象的な昔のこと」
- direct question
- topic closing
- correction
- disagreement

評価:

- unsupported claim 0
- prompt leak 0
- correction doubles-down 0
- unnecessary question rate
- response length
- natural Japanese human rating

---

# 45. Genesis Testing

Full 19-year Genesis を通常 CI 毎回実行しない。

3 tier:

```text
Unit: days/weeks
Integration: 1 year
Full Acceptance: 19 years
```

## Full Genesis acceptance

最低条件:

- 19 annual records
- 228 monthly records or configured exact count
- no age mismatch
- no future leakage detected
- no USER actor before FIRST_BOOT_COMPLETE
- NPC continuity audit pass
- nonzero simulated experiences
- nonzero appraisal
- nonzero memory encoding attempts
- some memories forgotten/not encoded
- some memories active
- periodic consolidation > 1
- personality/value changes only with sufficient evidence
- no target-personality backsolve field
- restart/resume from mid-year succeeds

---

# 46. Naturalness Evaluation

旧版・新型の blind comparison 用 scenario runner を用意する。

Metrics:

```text
Japanese naturalness
Turn appropriateness
Question necessity
Response length naturalness
Repetition/catchphrase rate
Relationship-appropriate register
Correction success
Unsupported claim rate
Silence appropriateness
Proactive naturalness
```

LLM-as-judge だけに依存せず OWNER human evaluation を残す。

---

# 47. Shadow Modes

危険な自律機能は first live から直接送信しない。

対象:

- proactive contact
- optional intentional silence
- autonomous NPC contacts
- autonomous web search

Mode:

```text
OFF
SHADOW
LIVE
```

Shadow decisions を Admin から確認可能にする。

---

# 48. Failure Policy

各 subsystem は failure mode を明示する。

## Conversation

- Appraisal fail → deterministic neutral fallback
- Social Interpretation fail → reply-safe fallback
- Memory rerank fail → strict lexical relevant memories only, or none
- Reply fail → suppress / retry once
- Grounding fail → suppress

## Diary

- generate fail → pending retry; sleep continues

## Autonomous

- LLM candidate fail → no action, not random action

## NPC

- generation fail → no new NPC interaction

## Search

- provider fail → unresolved knowledge, not fabricated answer

## Genesis

- high severity critic fail → stop/resume; FIRST_BOOT_COMPLETE prohibited

---

# 49. Security / Secrets / Privacy

- `.env`, token, raw secret を Debug へ出さない。
- Admin command は owner-only。
- LLM trace で system prompt full text を default 保存しない。
- Dialogue corpus raw content を license 条件に反して配布しない。
- backup / DB / logs は gitignore。

---

# 50. Claude Code 実装順序

依存関係順に進める。複数 Phase を同一巨大変更で実装しない。

## Phase 0 — Fresh Rebuild Foundation

- final backup
- fresh schema
- rebuild epoch
-旧 history import なし
- Implementation Ledger
- Capability Contracts

**Gate:** fresh DB boot + migrations + reset test。

## Phase 1 — Appraisal / Grounding / Correction

- categorical appraisal
- social correction detection
- GroundingContext
- ClaimGroundingGuard
- internal leak guard

**Gate:** 実 Ollama regression で既知 hallucination ケース pass。

## Phase 2 — Memory Retrieval v2

- candidate/relevance/accessibility separation
- LLM rerank
- recall modes
- practice semantics
- debug inspector

**Gate:** 無関係 memory positive-feedback を E2E で再現不可。

## Phase 3 — Human Conversation / Japanese

- SocialInterpretation
- SurfacePlan
- DialogueReferenceProvider
- JapaneseRealizer
- Common Ground

**Gate:** naturalness benchmark + no guard regression。

## Phase 4 — Response Intent / Silence

- intentional silence
- typing change
- silence event

**Gate:** eligible/noneligible silence matrix pass。

## Phase 5 — Admin / Debug

- router
- read-only commands
- no side effects tests

**Gate:** admin command leaves social state unchanged。

## Phase 6 — Autonomous Runtime

- runtime lifecycle
- opportunities
- next wakeup
- recovery

**Gate:** USERなし synthetic time で autonomous cycle records produced。

## Phase 7 — Activity / Sleep / Scheduler

- actual selection
- start/finish/interruption
- autonomous sleep/wake
- scheduler tick wiring

**Gate:** actual DB activities/sleep rows + events > 0。

## Phase 8 — NPC / Groups / Goal / Habit

- NPC generation/use
- relationships
- group activity
- goal/habit candidates

**Gate:** society vertical slice + independent relatedness source。

## Phase 9 — Proactive

- trigger source
- hard gate
- LLM judgment
- shadow
- live outbound path

**Gate:** shadow several scenarios + explicit live integration test。

## Phase 10 — Diary

- life day
- context
- generation
- persistence
- retry
- read activity

**Gate:** bedtime E2E + failure does not prevent sleep。

## Phase 11 — Search / Knowledge

- SearchProvider
- Tool Manager integration
- acquisition funnel

**Gate:** no fake search claim; chronology pass。

## Phase 12 — Genesis v2

- anchors
- annual
- monthly
- continuity
- critics
- extraction
- replay
- checkpoints
- final audit

**Gate:** 1-year test, then 19-year acceptance。

## Phase 13 — Full FIRST BOOT

Fresh DB で本番 Genesis を一度実行。

## Phase 14 — Shadow Runtime Evaluation

- silence/proactive/NPC/search shadow
- OWNER review

## Phase 15 — Live

Discord Character mode 開始。

---

# 51. Claude Code 1 Phase の作業手順

Claude Code は毎 Phase 次の順で行う。

## Step 1 Preflight

1. 本仕様の対象章を読む。
2. Implementation Ledger を確認。
3. 現コードの Authority / Writer / call path を確認。
4. 変更対象 file を列挙。
5. 既存 invariant test を確認。

## Step 2 Design Note

コード変更前に短く:

```text
Trigger
Current broken/missing path
New entry point
Authority
Event
Persistence
Failure path
Tests
```

を作る。

## Step 3 Implementation

1 responsibility per patch。

## Step 4 Unit tests

新ロジック。

## Step 5 Integration wiring

実際の bootstrap/runtime から到達すること。

## Step 6 E2E proof

DB / Event / output を確認。

## Step 7 Restart proof

該当機能が persistence を持つ場合 restart test。

## Step 8 Architecture reviewer

Single Writer / Authority / hidden coupling を確認。

## Step 9 Test reviewer

「テストが実装をなぞっているだけ」になっていないか確認。

## Step 10 Ledger update

E2E_VERIFIED になった Spec ID のみ更新。

---

# 52. Claude Code 禁止事項

1. 「class を追加したので完成」と報告しない。
2. Bootstrap instantiate だけで Runtime 接続済みと扱わない。
3. Unit test だけで Phase 完了にしない。
4. README のチェックボックスを先に埋めない。
5. TODO を残した required vertical path を完成扱いしない。
6. LLM が出した事実を無検証で DB へ commit しない。
7. LLM が出した人格値を直接 trait に入れない。
8. Candidate を Event にしない。
9. Debug を Character Event にしない。
10. Diary を Memory fallback にしない。
11. Corpus を YUI Memory にしない。
12. Scheduler から Discord を直接送信しない。
13. Runtime loop で毎秒 LLM を呼ばない。
14. NPC全員を full agent 化しない。
15. Genesis annual/monthly text を subjective memory に直挿ししない。
16. 最終人格へ逆算して Genesis を生成しない。
17. Critic failure を warning だけで通過しない。
18. FIRST_BOOT_COMPLETE 前に Discord Character mode を開始しない。
19. Prompt 内だけの禁止事項を safety guarantee としない。Python validation を併設する。
20. Model に年齢計算を任せない。
21. Future information を過去へ入れない。
22. 旧 DB を新人格へ import しない。

---

# 53. Acceptance Scenarios

## Scenario A — First contact after fresh Genesis

- USER relationship baseline から開始。
- 過去 NPC は存在してよい。
- USER に関する simulated memory はゼロ。
- 「初めまして」が自然。

## Scenario B — Age question

USER: 「何歳だっけ？」

- autobiographical retrieval mode
- relevant Genesis memory/self fact
- correct age
- future-leak summaryを引用しない

## Scenario C — Today activity

USER: 「今日は何してた？」

- completed Activity のみ話す
- 0件なら正直に何もしていない/大したことしていない方向
- candidate/plan を completed と言わない

## Scenario D — Correction

YUI が誤解 → USER「詩？」

- possible correction detection
- common ground contested
- unsupported previous claim を retract
- double down しない

## Scenario E — Closure silence

USER: 「うん」

- context が自然な終了なら silence が可能
- typingしない
- intentional silence event

## Scenario F — Direct question cannot silence

USER: 明確な質問

- hard gate blocks silence

## Scenario G — Spontaneous memory

activity cue
→ old memory recalled
→ emotion change
→ optionally proactive shadow candidate

## Scenario H — NPC life

group activity
→ NPC interaction
→ relationship update
→ USER がいなくても生活変化

## Scenario I — Diary

major sleep
→ diary generated
→ persisted
→ memoryとは別

翌月 memory が忘れた出来事を質問されても diary を自動検索しない。

## Scenario J — Diary reading

YUI が自ら昔の日記を読む activity
→ old information encountered
→ emotion/new memory possible

## Scenario K — Proactive backoff

YUI proactive sent, USER ignores
→ interval widens
→ second ignore
→ stop until USER replies

## Scenario L — Search

YUI does not know
→ selector chooses search
→ Tool succeeds
→ known result

Tool fails:
→「検索した結果〜」とは言わない。

## Scenario M — Restart during activity

activity ongoing
→ process crash
→ restart
→ recovery correctly finishes/resumes/interrupts
→ duplicate completionなし

## Scenario N — Restart during Genesis

year 8 month 4
→ process terminated
→ resume
→ prior months not duplicated
→ replay not duplicated

## Scenario O — 19-year Genesis

full run
→ 19 annual + 228 monthly records
→ critics pass
→ experiences replayed
→ selective memories
→ some forgetting
→ personality derived
→ FIRST_BOOT_COMPLETE

---

# 54. Global Definition of Done

この再構築は以下をすべて満たして初めて完成。

## Conversation

- unsupported self-experience claim 0 in acceptance suite
- prompt/internal leak 0
- malformed JSON residue 0
- correction regression pass
- natural Japanese blind evaluation improves over current baseline
- not every turn asks a question
- intentional silence works only where allowed

## Memory

- relevance-first retrieval
- no high-accessibility irrelevant domination
- retrieval != recall practice
- spontaneous recall works
- archive never normal fallback

## Internal psychology

- appraisal schema stable
- personality changes only deep path
- user trait inference requires independent evidence
- relationship no rapid saturation

## Autonomous life

- runtime loop actually running
- activities actually occur
- sleep actually occurs
- scheduler actually ticks
- goals/habits participate in decisions

## NPC

- actual NPC rows/interactions in E2E
- objective vs subjective model separated
- groups participate in relatedness

## Proactive

- shadow/live pathways tested
- unanswered backoff
- grounded reason

## Diary

- daily major-sleep diary
- external record, not perfect memory
- read activity works
- diary failure does not block sleep

## Search

- Tool-authoritative
- no fake search claims

## Genesis

- detailed annual/monthly generation
- continuity ledger
- multiple critics
- event extraction + true replay
- selective memory + forgetting
- NPC continuity
- checkpoint/resume
- no future leakage
- no USER before FIRST_BOOT_COMPLETE

## Engineering

- Implementation Ledger all required IDs E2E_VERIFIED
- no required dead subsystem
- restart recovery tests
- real Ollama smoke
- fresh DB full FIRST BOOT accepted

---

# 55. 最終的なシステム像

完成後の YUI は「大量の設定を prompt に入れたキャラクター」ではなく、次の循環を持つ。

```text
World / USER / NPC
      ↓
Objective Event
      ↓
Appraisal
      ↓
Emotion / Mood / Needs
      ↓
Memory / Belief / Self / Relationship
      ↓
Goals / Habits / Values
      ↓
LLM social/cognitive judgment
      ↓
Action candidates
      ↓
Python Decision / Evidence / Policy
      ↓
Actual action
      ↓
Outcome
      ↓
new Event / Memory / Consolidation
      ↓
slow Personality / Narrative change
      ↓
next experience
```

会話はこの内部を自然な日本語へ翻訳する一つの行動に過ぎない。

YUI は USER がいない時にも、低コストで生活し、眠り、活動し、NPC と関わり、思い出し、考え、必要なら検索し、一日の終わりに日記を残す。

FIRST BOOT ではこの同じ因果系を 19 年の仮想発達史へ高精度に適用し、その結果として現在の YUI が形成される。

**この仕様の最重要原則は、「部品が存在する」ことではなく、「因果の入口から出口まで本当に動き、その事実を Event・DB・Debug・E2E test で証明できること」である。**


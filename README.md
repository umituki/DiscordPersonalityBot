# YUI v2

単一ユーザー向け Discord Companion BOT「YUI」。固定キャラクターを演じ続ける BOT ではなく、
記憶・忘却・感情・関係・目標・仮想生活・人格変化を長期間維持する継続的なデジタル人格システム。

正式仕様: [`docs/YUI_v2_SPEC.md`](docs/YUI_v2_SPEC.md)
実装規則: [`CLAUDE.md`](CLAUDE.md), [`.claude/rules/`](.claude/rules)

## 現在の実装状況

仕様 Section 35 のロードマップに従い、順番に実装している。現在は **Phase 9（Growth）まで**完了。Phase 10 以降は未着手。

| Phase | 内容 | 状態 |
|---|---|---|
| 0 | config / bootstrap / CLI | 実装済み |
| 1 | SQLite / Event / State transaction core | 実装済み |
| 2 | Ollama structured LLM | 実装済み |
| 3 | Basic Discord conversation + Output Guard | 実装済み |
| 4 | Context / Episode / Memory | 実装済み |
| 5 | Immediate Psychology（Appraisal / Emotion / Mood / Needs） | 実装済み |
| 6 | Social / Belief / Self | 実装済み |
| 7 | Agency（Goals / Decision / Dialogue Act / Habits / Epistemics / Tools） | 実装済み |
| 8 | Virtual Life（World / Activity / Sleep / Scheduler / Proactive） | 実装済み |
| 9 | Growth（Consolidation / Adaptations / Personality / Values / Narrative / Drift） | 実装済み |
| 10+ | Society / Genesis / Hardening | 未着手 |

### Phase 1 で動作するパイプライン

```text
Event
→ EventStore (append-only)
→ S0 Snapshot
→ Dispatcher (idempotent delivery)
→ Subscriber が StateChangeProposal を返す
→ StateArbitrator (ownership / layer / policy 検証)
→ StateCommitter (単一 transaction で state + history + failure + delivery)
```

### Phase 2 で動作する LLM 境界

```text
LLMRequest
→ OllamaClient (concurrency 1 / timeout)
→ Parse → Schema → Semantic → State → Identity  (spec 28.2)
→ 受理された候補のみ呼び出し側へ
```

不正出力は値として返らず `failures` に記録される。prompt は `config/prompts/`
の versioned file、model / prompt version は runtime manifest と `llm_calls` に記録。

### Phase 3 で動作する会話

```text
Discord message
→ Adapter (USER 判定 / admin 分離)
→ USER_MESSAGE_RECEIVED event → run (persist + snapshot + commit)
→ Context (identity REQUIRED + 直近ログ IMPORTANT)
→ Ollama structured 生成
→ Output Guard (身体行動・未実行ツール・内部漏洩を拒否)
→ 送信成功後にだけ YUI_MESSAGE_SENT を記録
```

Guard に拒否された文は送信されず、`YUI_REPLY_SUPPRESSED` として理由だけ残る。
拒否された本文は event に入らない（記憶に混入させないため）。

### Phase 4 で動作する記憶

```text
Events
→ Episode segmentation（沈黙・件数・時間で区切る）
→ Encoding Gate（novelty / 感情 / 相手志向 / 内容量で選別）
→ Episodic Memory（FTS5 trigram + accessibility）
→ 想起（関連度 + accessibility + 新しさ + 感情 + 重要度）
→ 想起によって accessibility 上昇（逓減あり）
→ 時間経過で accessibility 低下（削除はしない）
```

Objective Archive（events）と Subjective Memory は分離されている。符号化されな
かった出来事は、archive に残っていても思い出せない。

### Phase 5 で動作する心理

```text
Event
→ Appraisal（8 次元。LLM は候補、Python が採否と confidence 上限を決める）
→ Emotion Engine  : 強度と持続を分離、複数同時可、閾値未満は「起きなかった」
→ Mood Engine     : 感情より遅い。慣性と baseline 回帰を持つ別 state
→ Need Engine     : 孤独と「一人でいたい」は独立に動く
→ Proposal → Arbitration → 単一 transaction
```

同じ出来事が常に同じ感情になるわけではない。Appraisal が文脈で変わるため。
LLM が使えないときは appraisal が既定値へ degrade し、状態は壊れない。

### Phase 6 で動作する社会的認知

```text
Appraisal + Event
→ Relationship : 接触量で上がるのは familiarity だけ。trust は証拠が要る
                 謝罪は conflict_residue を下げるが trust は 1 も戻さない
→ Attachment   : activation / felt_security と「一般的傾向」を分離
                 security が高いほど分離に強く、依存は増えない
→ Social Cogn. : 「いまそう見える」と「そういう人だ」を分離
                 一度の短いメッセージで trait は動かない（証拠 6 件が必要）
→ Beliefs      : 確信度は証拠から導出。支持と反証の両方を保持し、
                 同一一次ソースの再掲は 1 件としてしか数えない
→ Self Model   : 自己像は行動より遅れて変わる。証拠が混ざると
                 自己像が揺れるのではなく明瞭さ(clarity)が下がる
```

### Phase 7 で動作する行為選択

```text
Tool Manager  : 実行の真偽を握る唯一の場所。失敗は失敗として残り、
                Output Guard はこの記録だけを信じる
Goals         : need から目標が生まれる。plan と「起きたこと」は別物
Habits        : 回数ではなく cue × 反復。1 日空けてもリセットしない。
                文脈が消えても痕跡は残る
Decision      : 僅差のときだけ小さく揺らぐ。大差は必ず良い方を選ぶ
Dialogue Act  : 文章の前に「どう応じるか」を決めてから書く
Epistemics    : 知らない ≠ 自動検索。相手が居れば聞く、失敗はしばらく再試行しない
```

### Phase 8 で動作する仮想生活

```text
World Service : YUI が「いま何をしているか」の唯一の writer。
                活動は開始と終了が別で、終了して初めて「起きたこと」になる
Sleep         : 時計規則ではなく two-process（睡眠圧 × 概日リズム）＋
                目標と会話による抵抗。抵抗は入眠を遅らせるが永久には防げない
Catch-up      : 停止中の時間は捏造しない。上限付きで圧縮再構成するだけ
Scheduler     : job は state を書かない。Opportunity を出すだけ。
                再起動後も未処理 job は復元される
Proactive     : 無返信が増えるほど送信間隔が伸びる（positive feedback の禁止）。
                上限を超えると USER が話すまで一切送らない
```

### Phase 9 で動作する成長

```text
Consolidation : 出来事にその場で反応するのではなく、既に commit された
                state 変化とその provenance を後から読み直す別ジョブ
Adaptations   : 特性より速く動く中間層。証拠が溜まって初めて動く。
                端に寄るほど同方向の証拠は効かなくなる（戻る方向は減衰しない）
Deep Gate     : 反復・持続・複数文脈・意味のある結果・気分で説明できないこと。
                5 条件すべてを満たすまで Trait も Value も 1 も動かない
Personality   : baseline / adaptation / 表出を分離。1 回の更新は最大 0.01。
                baseline は表出を追って少しずつ動く（初期値は永久固定ではない）
Values        : 相対優先度。ひとつ上げると必ず他が下がる。総和は保存される
Narrative     : 繰り返し思い出される話題が自己物語になる。飽和する
Drift Monitor : 測って分類するだけ。EXPECTED / SUSPICIOUS / INVALID。
                正常な人生変化を clamp で消さない
```

数か月ぶんの一貫した証拠が揃って初めて Trait が 0.01 動く。動いた事実は
`personality_history` に candidate と run とともに残る。commit が受理しなかった
変化は台帳にも履歴にも書かれない。

主要な不変条件はすべてテストで保護している（`tests/invariants/`）。

## セットアップ

```bash
python -m venv .venv           # Python 3.12+
.venv/bin/pip install -e ".[dev]"
cp .env.example .env           # secrets は .env のみ。commit しない
# DISCORD_BOT_TOKEN と DISCORD_OWNER_USER_ID を設定すると会話が有効になる
```

## 実行

```bash
yui migrate     # DB migration の適用
yui status      # schema / manifest / event 数 / integrity の確認
yui run         # 起動。token と owner id があれば Discord に接続する
```

`config/settings.yaml` が静的設定、`config/policies/` が調整値（仕様 40）、
`.env` が secret。動的な life state は `data/` の SQLite のみが Authority。

## テスト

```bash
.venv/bin/python -m pytest              # 全件
.venv/bin/python -m pytest -m invariant # 不変条件のみ
```

テストは一時 DB のみを使用し、production `data/` には触れない。

## ディレクトリ

```text
app/
  config.py bootstrap.py main.py clock.py ids.py
  context/       Context builder (REQUIRED / IMPORTANT / OPTIONAL)
  conversation/  engine / guard / service / projection
  events/        Event model / store / bus / dispatcher
  memory/        segmentation / encoding / retrieval / forgetting / engine
  psychology/    appraisal / emotion / mood / needs
  agency/        goals / plans / habits / decision
  epistemics/    epistemic action selection
  world/         activity / sleep / world service（仮想生活の single writer）
  jobs/          scheduler / proactive contact
  consolidation/ adaptations / deep gate / personality / values / narrative / drift
  social/        relationship / attachment / user model / beliefs / self model
  tools/         Tool Manager / registry / builtin tools
  interfaces/    discord/ (adapter, dto, gateway)
  llm/           LLMClient / Ollama / prompts / structured / validation
  state/         proposal / snapshot / ownership / dependency_graph / arbitrator / committer / policy
  storage/       Database / migrations / repositories（SQL の唯一の境界）
  orchestrator/  RunContext / RunView / EventProcessor
  versioning/    RuntimeManifest
  observability/ logging
  resources/     static identity loading
character/       identity.yaml, immutable_rules.yaml, speech.md
config/          settings.yaml, policies/, prompts/
docs/            YUI_v2_SPEC.md
tests/           unit/, invariants/
```

## 開発上の約束

- LLM は人格・DB 状態の Authority ではない。Python と SQLite が Authority。
- 各 state domain には Single Writer がある。他 module は Proposal を出す。
- 不正な Proposal は clamp せず reject し、`failures` に記録する。
- Event は immutable。訂正は新しい Event（`EVENT_INVALIDATED` 等）で行う。
- LLM 出力は候補にすぎず、検証を通らなければ値として返らない。
- 先のフェーズを先取り実装しない。

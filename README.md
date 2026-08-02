# YUI v2

単一ユーザー向け Discord Companion BOT「YUI」。固定キャラクターを演じ続ける BOT ではなく、
記憶・忘却・感情・関係・目標・仮想生活・人格変化を長期間維持する継続的なデジタル人格システム。

正式仕様: [`docs/YUI_v2_SPEC.md`](docs/YUI_v2_SPEC.md)
実装規則: [`CLAUDE.md`](CLAUDE.md), [`.claude/rules/`](.claude/rules)
実機セットアップ: [`docs/SETUP.md`](docs/SETUP.md)
既存 DB の修復: [`docs/REPAIR.md`](docs/REPAIR.md)
実機検証由来の修正仕様: [`docs/YUI_v2_bugfix_patch_spec.md`](docs/YUI_v2_bugfix_patch_spec.md)

## 現在の実装状況

仕様 Section 35 のロードマップに従い、Phase 0 から順番に実装した。**全 13 フェーズ（Phase 0〜12）実装済み**。

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
| 10 | Society（NPC / Groups / Network / Lifecycle） | 実装済み |
| 11 | Genesis（Historical Knowledge / Past Simulation / FIRST BOOT） | 実装済み |
| 12 | Production Hardening（Admin / Backup / Resources / Chaos / Launcher） | 実装済み |

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

### Phase 10 で動作する仮想社会

```text
NPC Tier      : 全 NPC を完全 Agent 化しない。Tier 0 は「居るだけ」で
                関係 state もモデルも持たない。Tier 2 だけがモデルを持つ
Profile ≠ Model: NPC の客観プロファイルと「YUI がそう思っていること」は
                別テーブル・別 writer。YUI のモデルは間違っていてよい
USER / NPC    : 別ドメイン・別 writer・別 origin。NPC event は USER 関係を
                1 も動かさず、USER の発言は NPC state を 1 も動かさない
Lifecycle     : unmet / acquaintance / familiar / close / strained /
                distant / dormant / ended / reconnected。
                連絡が途絶えること（distant→dormant）と、こじれること
                （strained）と、終わらせること（ended）は別物。
                ended は決定であって、時間の結果ではない
Groups        : 所属は relatedness の供給源。USER だけが関係性の源では
                なくなる。参加していない集まりは所属にならない
Network       : NPC 同士も繋がる。社会は YUI を中心とした星型ではない
```

### Phase 11 で動作する生成（Genesis）

```text
Knowledge Builder : 「世界が何をいつ知っていたか」を検証可能な形で記録する。
                    available_from のない候補は既定値で埋めずに拒否する
Temporal Guard    : その時点より後にしか存在しない情報は Exposure に入れない。
                    後年の資料を「当時公表されていた証拠」に使うのは可。
                    違反は握りつぶさず例外にする
Exposure Funnel   : 存在した → 機会 → 届いた → 気づいた → 気になった →
                    理解できた → 符号化された → 保持された。
                    どの段階でも止まる。有名だっただけでは「知っている」に
                    ならない
Knows()           : acquisition record だけを根拠にする。LLM が事前学習で
                    知っていることは YUI が知っている根拠にしない
Seed              : 7 問の回答が作れるのは Temperament だけ。どの回答も
                    中央値から一定幅しか動かせず、極端に固定できない。
                    価値観・習慣・完成人格は Simulation の結果
Experience        : Routine が大半。Major は年あたり上限、Turning Point は
                    生涯上限、否定的な Major の比率にも上限がある
                    （つらい出来事を人格の説明装置にしない）
Simulation        : 通常の processor / appraisal / arbitrator / transaction を
                    そのまま通す。第二の人格エンジンは作らない。
                    最終人格を指定する引数は存在しない
FIRST BOOT        : Deep Consolidation → 整合性 → 知識年代 → 同一性 →
                    ドリフト → 品質。全部通ったときだけ FIRST_BOOT を出す。
                    それ以前に USER との関係経験は 1 件も作らない
```

### Phase 12 で動作する運用

```text
Admin Plane   : Character Mode と Admin Mode を完全分離。会話の「忘れて」は
                suppress であって hard delete ではない。
                YUI 自身に Admin 権限はない
Destructive   : 影響分析 → dry-run → 確認 → 検証済みスナップショット →
                変更 → 波及再評価 → 検証 → 監査。
                確認がなければ実行されず、スナップショットが検証できなければ
                何も変更しない
Backup        : SQLite の backup API を使う（Explorer コピーは backup ではない）。
                作成後に integrity を検証し restore test を行い、
                通らなかったものは「使える backup」として返さない。
                live DB がクラウド同期フォルダにあれば起動時に警告する
Resources     : P0 応答 … P7 過去シミュレーションの優先度キュー。
                同順位は到着順。USER の入力が来たら background は
                安全な checkpoint で譲る（途中で殺さない）
Startup       : 復旧 → world catch-up → scheduler restore → readiness の順。
                停止中に経過した時間をなかったことにしない
Chaos         : subscriber 例外・commit 失敗・二重配送・モデル不達・
                プロセス強制終了を注入しても state は壊れない
```

主要な不変条件はすべてテストで保護している（`tests/invariants/`, `tests/chaos/`）。

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
yui backup      # SQLite backup API で取得し、検証と restore test を行う
yui run         # 起動。token と owner id があれば Discord に接続する

# Windows 本番機
powershell -File scripts/yui.ps1 -Command run
```

`config/settings.yaml` が静的設定、`config/policies/` が調整値（仕様 40）、
`.env` が secret。動的な life state は `data/` の SQLite のみが Authority。

## テスト

```bash
.venv/bin/python -m pytest              # 全件
.venv/bin/python -m pytest -m invariant # 不変条件のみ（仕様 34.2）
.venv/bin/python -m pytest -m scenario  # 必須シナリオのみ（仕様 34.3）
.venv/bin/python -m pytest -m chaos     # 障害注入のみ（仕様 34.1）
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
  society/       npc / npc relationship / groups / social network / lifecycle
  knowledge/     historical knowledge / temporal guard / exposure funnel
  simulation/    temperament seed / experiences / past simulation / genesis
  admin/         admin control plane / risk classes（spec 30）
  reliability/   resource manager（LLM priority queue, spec 33）
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
tests/           unit/, invariants/, scenarios/, chaos/
```

## 開発上の約束

- LLM は人格・DB 状態の Authority ではない。Python と SQLite が Authority。
- 各 state domain には Single Writer がある。他 module は Proposal を出す。
- 不正な Proposal は clamp せず reject し、`failures` に記録する。
- Event は immutable。訂正は新しい Event（`EVENT_INVALIDATED` 等）で行う。
- LLM 出力は候補にすぎず、検証を通らなければ値として返らない。
- 先のフェーズを先取り実装しない。

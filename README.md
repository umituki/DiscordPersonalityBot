# YUI v2

単一ユーザー向け Discord Companion BOT「YUI」。固定キャラクターを演じ続ける BOT ではなく、
記憶・忘却・感情・関係・目標・仮想生活・人格変化を長期間維持する継続的なデジタル人格システム。

正式仕様: [`docs/YUI_v2_SPEC.md`](docs/YUI_v2_SPEC.md)
実装規則: [`CLAUDE.md`](CLAUDE.md), [`.claude/rules/`](.claude/rules)

## 現在の実装状況

仕様 Section 35 のロードマップに従い、順番に実装している。現在は **Phase 4
（Context / Episode / Memory）まで**完了。Phase 5 以降は未着手。

| Phase | 内容 | 状態 |
|---|---|---|
| 0 | config / bootstrap / CLI | 実装済み |
| 1 | SQLite / Event / State transaction core | 実装済み |
| 2 | Ollama structured LLM | 実装済み |
| 3 | Basic Discord conversation + Output Guard | 実装済み |
| 4 | Context / Episode / Memory | 実装済み |
| 5+ | Psychology / Social / Agency / Life / Growth / Society / Genesis | 未着手 |

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
  interfaces/    discord/ (adapter, dto, gateway)
  llm/           LLMClient / Ollama / prompts / structured / validation
  state/         proposal / snapshot / ownership / dependency_graph / arbitrator / committer / policy
  storage/       Database / migrations / repositories（SQL の唯一の境界）
  orchestrator/  RunContext / EventProcessor
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

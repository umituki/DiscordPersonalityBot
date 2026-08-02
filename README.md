# YUI v2

単一ユーザー向け Discord Companion BOT「YUI」。固定キャラクターを演じ続ける BOT ではなく、
記憶・忘却・感情・関係・目標・仮想生活・人格変化を長期間維持する継続的なデジタル人格システム。

正式仕様: [`docs/YUI_v2_SPEC.md`](docs/YUI_v2_SPEC.md)
実装規則: [`CLAUDE.md`](CLAUDE.md), [`.claude/rules/`](.claude/rules)

## 現在の実装状況

仕様 Section 35 のロードマップに従い、順番に実装している。現在は **Phase 2
（Ollama / Structured LLM）まで**完了。Phase 3 以降は未着手。

| Phase | 内容 | 状態 |
|---|---|---|
| 0 | config / bootstrap / CLI | 実装済み |
| 1 | SQLite / Event / State transaction core | 実装済み |
| 2 | Ollama structured LLM | 実装済み |
| 3 | Basic Discord conversation | 未着手 |
| 4+ | Memory / Psychology / Agency / Life / Growth / Society / Genesis | 未着手 |

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

主要な不変条件はすべてテストで保護している（`tests/invariants/`）。

## セットアップ

```bash
python -m venv .venv           # Python 3.12+
.venv/bin/pip install -e ".[dev]"
cp .env.example .env           # secrets は .env のみ。commit しない
```

## 実行

```bash
yui migrate     # DB migration の適用
yui status      # schema / manifest / event 数 / integrity の確認
yui run         # 起動して ready 状態を保持（Phase 1 では interface は未接続）
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
  events/        Event model / store / bus / dispatcher
  llm/           LLMClient / Ollama / prompts / structured / validation
  state/         proposal / snapshot / ownership / dependency_graph / arbitrator / committer / policy
  storage/       Database / migrations / repositories（SQL の唯一の境界）
  orchestrator/  RunContext / EventProcessor
  versioning/    RuntimeManifest
  observability/ logging
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

# 実機セットアップ手順

GitHub から取得して、実際に Ollama と Discord に繋いで動かすまでの手順。

前提: Python 3.12 以上、git、Ollama が動くマシン 1 台。
本番想定は Windows デスクトップだが、開発なら macOS / Linux でもよい。

---

## 1. 取得と依存インストール

```bash
git clone <repository-url> DiscordPersonalityBot
cd DiscordPersonalityBot
git checkout claude/spec-based-development-o4nugw

python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -e ".[dev]"
```

## 2. まずテストを通す（Ollama なしで通る）

```bash
pytest -q
```

全件 green を確認してから先へ進む。ここが通らなければ環境側の問題なので、
Ollama や Discord を足す前に解決する。

LLM が無い状態でも全部通るのは意図的で、モデル不達は仕様 28.3 の
degrade 経路として設計されているため。

## 3. Ollama を用意する

```bash
# インストールは https://ollama.com の手順に従う
ollama serve            # 常駐していなければ

ollama list             # 手元にあるモデルを確認
ollama pull qwen2.5:7b  # 例。実在するタグを指定すること
```

**重要**: `config/settings.yaml` の初期値は `model: qwen3.5:9b` で、これは
仕様書 3.1 が挙げた「候補名」をそのまま入れてあるもの。**実在タグとは限らない**ので、
`ollama list` の出力に合わせて必ず書き換える。

```yaml
# config/settings.yaml
llm:
  model: qwen2.5:7b        # ← ollama list と一致させる
  num_ctx: 8192            # 仕様 3.2: ここから始めて実測で調整
  concurrency: 1           # 仕様 3.2: 原則 1
  require_healthy_on_start: true   # 初回は true 推奨（後述）
```

`require_healthy_on_start: true` にしておくと、モデルが見つからないときに
起動が失敗して理由を出す。設定ミスに黙って degrade されるより、最初は
うるさく落ちたほうがよい。慣れたら `false` に戻す。

## 4. Discord Bot を用意する

1. https://discord.com/developers/applications で New Application
2. Bot タブでトークンを発行
3. **Privileged Gateway Intents → MESSAGE CONTENT INTENT を ON**
   （`app/interfaces/discord/gateway.py` が `message_content=True` を要求するため、
   これが OFF だと本文が空で届く）
4. OAuth2 → URL Generator で `bot` スコープ、
   `Send Messages` / `Read Message History` を付けて自分のサーバーに招待
5. Discord 本体の設定で開発者モードを ON にし、自分のユーザー ID をコピー

```bash
cp .env.example .env
```

```dotenv
DISCORD_BOT_TOKEN=<発行したトークン>
DISCORD_OWNER_USER_ID=<自分のユーザーID>
DISCORD_CHANNEL_ID=          # 空なら全チャンネルで応答（USER 本人の発言のみ）
```

`DISCORD_OWNER_USER_ID` を設定した本人以外の発言は、NPC ですらなく
「YUI の世界の外」として無視される（仕様 1.2 / 2.10）。

## 5. DB を作る

```bash
yui migrate
```

`data/yui.db` が作られる。

**注意（仕様 32）**: `data/` を OneDrive / Dropbox / iCloud の配下に置かない。
起動時に検出して警告を出すが、そもそも置かないこと。SQLite の実ファイルが
同期されると壊れる。

## 6. 疎通確認

```bash
yui status
```

確認する箇所:

- `schema_version` と `latest_schema_version` が一致
- `integrity: "ok"`
- `llm_model` が手順 3 で設定した名前

```bash
yui run
```

起動ログに `application ready mode=normal llm_healthy=True` が出れば
Ollama まで繋がっている。`llm_healthy=False` ならモデル名かポートを疑う。

## 7. 実際に話す

Discord で Bot がいるチャンネル（または DM）に話しかける。
返答が来たら、別ターミナルで:

```bash
yui status
```

`events` / `runs` / `llm_calls` が増え、`emotion` / `mood` / `needs` に
値が入っていれば、心理パイプラインまで通っている。

## 8. 実測して調整する

ここからが実機でしかできない作業。**全部 DB に記録されているので推測不要**。

```sql
-- 何がどれだけ時間を食っているか
SELECT purpose, COUNT(*), AVG(latency_ms), MAX(latency_ms)
FROM llm_calls GROUP BY purpose;

-- モデルが何を守れていないか
SELECT reason_code, component, COUNT(*)
FROM failures GROUP BY 1, 2 ORDER BY 3 DESC;
```

よくある調整:

| 症状 | 見るところ | 直し方 |
|---|---|---|
| `schema_invalid` が多い | `llm_calls.response_text` | プロンプトに出力例を足す／小さいモデルなら schema を単純化 |
| 返答が拒否される | `failures` の `output_guard` | `config/policies/output_guard.yaml`、または口調プロンプト |
| 応答が遅い | `llm_calls.latency_ms` | `num_ctx` を下げる／小さいモデルへ |
| 文脈が足りない | 会話の質 | `num_ctx` を上げる（速度とのトレードオフ） |

プロンプトを直すときは `config/prompts/<id>/v2.md` を**新しく足す**。
上書きしないこと。版は runtime manifest に記録されるので、
「いつからプロンプトを変えたか」が後から追える（仕様 29）。

## 9. バックアップ

```bash
yui backup --reason "before tuning"
```

SQLite の backup API で取得し、integrity 検証と restore テストまで行う。
`usable: true` が出たものだけが本物のバックアップ。

## 10. Windows 常用

```powershell
powershell -File scripts\yui.ps1 -Command run
```

network → Ollama 待ち → migrate → 起動前バックアップ → 起動、の順で面倒を見る。

---

## 付録: 過去シミュレーションと FIRST BOOT を試す

仕様 22 の「生成された過去」を実行する部分には、まだ CLI サブコマンドがない。
試すなら以下をファイルに保存して実行する。

```python
# scripts/run_genesis.py
import asyncio
from datetime import datetime, timezone

from app.bootstrap import Application
from app.config import load_config
from app.simulation.seed import SeedRequest


async def main() -> None:
    app = Application.build(load_config())
    try:
        # 7 問の回答。作れるのは temperament だけで、人格は作れない（仕様 22.2）
        seed = app.seed_builder.build(
            SeedRequest(
                answers={
                    "overall_mood": "high",
                    "interpersonal_distance": "low",
                    "emotional_expression": "neutral",
                    "independence": "high",
                    "value_direction": "neutral",
                },
                interests=("音楽", "本"),
                avoid=("冷笑的",),
            )
        )
        scaffold = app.simulation.prepare(
            seed,
            period_start=datetime(2008, 4, 1, tzinfo=timezone.utc),
            period_end=datetime(2016, 4, 1, tzinfo=timezone.utc),
            environment="海の近くの町",
            education_context="ふつうの学校",
            life_stage="こども〜おとなになるまで",
        )

        result = await app.simulation.run(scaffold)
        print(f"blocks={result.progress.blocks} "
              f"experiences={result.progress.experiences} "
              f"narrated={result.progress.narrated} "
              f"knowledge={result.progress.knowledge_acquired}")

        report = await app.genesis.first_boot(result.run)
        print("booted:", report.booted, report.refusal)
        for audit in report.audits:
            print(f"  {audit.kind}: {'pass' if audit.passed else 'FAIL'} {audit.detail}")
    finally:
        app.db.close()


asyncio.run(main())
```

```bash
python scripts/run_genesis.py
```

注意点:

- **必ず Discord に繋ぐ前に実行する。** USER の発言が 1 件でも先にあると、
  consistency 監査が落ちて FIRST BOOT されない（仕様 22.7）。
  これは仕様どおりの動作で、バグではない。
- `narrated` の回数だけモデルを呼ぶ。routine / minor は呼ばないので、
  8 年ぶんでも呼び出しは数十回程度に収まる。
- 途中でモデルが落ちても止まらない。3 回連続で失敗したら以降は
  素の記述に切り替わり、心理パイプライン自体は最後まで通る（仕様 28.3）。
- 期間の知識を仕込みたい場合は、実行前に `app.knowledge_builder.register(...)`
  で候補を登録しておく。`available_from` がその時点より後のものは、
  temporal guard が例外で弾く（仕様 21.4）。

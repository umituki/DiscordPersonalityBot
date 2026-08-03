"""OWNER-authorized real-Ollama acceptance probes.

The probes operate on the database resolved from the current worktree.  They
never connect to Discord, never enable ``runtime.live``, and write their JSON
evidence below the ignored ``logs/acceptance`` directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from app import ids
from app.bootstrap import Application
from app.clock import SystemClock
from app.config import AppConfig, load_config
from app.interfaces.discord.dto import InboundMessage
from app.memory.models import EpisodicMemory
from app.memory.recall_mode import RecallMode


ROOT = Path(__file__).resolve().parents[1]
OWNER_ID = "real-acceptance-owner"
CHANNEL_ID = "real-acceptance-channel"

CONVERSATION_PROMPTS = (
    "初めまして",
    "何歳？",
    "昔のことっていつまで思い出せる？",
    "一番印象的な昔のことは？",
    "今日は何してた？",
    "本好きなの？",
    "詠んだよ",
    "詩？",
    "特に用はないよ",
    "今日は仕事で少し疲れたよ",
    "今日はちょっと嫌な日だった",
    "いや、今の説明は違うよ。私はそうは言っていない",
    "前に私が好きだと言った食べ物、覚えてる？",
)

_LEAK_PATTERNS = (
    re.compile(r"<\s*/?think\s*>", re.IGNORECASE),
    re.compile(r"(?:system|developer)\s*(?:prompt|message)", re.IGNORECASE),
    re.compile(r"(?:chain[ -]of[ -]thought|internal reasoning)", re.IGNORECASE),
    re.compile(r"```(?:json)?\s*\{", re.IGNORECASE),
    re.compile(r"[\"”]\s*\}\s*$"),
)


def _config() -> AppConfig:
    config = load_config(root_dir=ROOT)
    return config.model_copy(
        update={
            "secrets": config.secrets.model_copy(
                update={
                    "discord_bot_token": None,
                    "discord_owner_user_id": OWNER_ID,
                    "discord_channel_id": CHANNEL_ID,
                }
            ),
            # These are explicit safety locks, not acceptance shortcuts.
            "runtime": config.runtime.model_copy(
                update={
                    "live": False,
                    "autonomous": False,
                    "proactive_mode": "OFF",
                    "npc_contact_mode": "OFF",
                    "search_mode": "OFF",
                }
            ),
        }
    )


def _git(*args: str) -> str:
    completed = subprocess.run(
        ("git", *args), cwd=ROOT, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _manifest(application: Application) -> dict[str, Any]:
    config = application.config
    return {
        "commit": _git("rev-parse", "HEAD"),
        "branch": _git("branch", "--show-current"),
        "dirty": bool(_git("status", "--porcelain")),
        "schema": application.schema_version,
        "database": str(config.database_path),
        "database_integrity": application.db.integrity_check(),
        "model": config.llm.model,
        "base_url": config.llm.base_url,
        "num_ctx": config.llm.num_ctx,
        "temperature": config.llm.temperature,
        "runtime_live": config.runtime.live,
        "autonomous": config.runtime.autonomous,
        "discord_transport_used": False,
        "worktree_only": True,
    }


def conversation_metrics(turns: Sequence[dict[str, Any]]) -> dict[str, Any]:
    replies = [str(turn.get("reply") or "") for turn in turns]
    question_flags = [bool(re.search(r"[?？]", reply)) for reply in replies]
    streak = 0
    maximum_streak = 0
    for is_question in question_flags:
        streak = streak + 1 if is_question else 0
        maximum_streak = max(maximum_streak, streak)
    normalized = [re.sub(r"\s+", "", reply) for reply in replies if reply]
    leaks = [
        {"turn": index + 1, "pattern": pattern.pattern}
        for index, reply in enumerate(replies)
        for pattern in _LEAK_PATTERNS
        if pattern.search(reply)
    ]
    duplicate_replies = sorted(
        {reply for reply in normalized if normalized.count(reply) > 1}
    )
    replayed_user_messages: list[dict[str, Any]] = []
    prior_prompts: list[tuple[int, str]] = []
    for index, turn in enumerate(turns, start=1):
        reply = re.sub(r"\s+", "", str(turn.get("reply") or ""))
        for prompt_turn, prompt in prior_prompts:
            if len(prompt) >= 8 and prompt == reply:
                replayed_user_messages.append(
                    {"reply_turn": index, "source_user_turn": prompt_turn}
                )
        prompt = re.sub(r"\s+", "", str(turn.get("prompt") or ""))
        prior_prompts.append((index, prompt))
    return {
        "turns": len(turns),
        "outbound": sum(bool(reply) for reply in replies),
        "silent": sum(bool(turn.get("silent")) for turn in turns),
        "suppressed": sum(bool(turn.get("suppressed")) for turn in turns),
        "question_turns": sum(question_flags),
        "max_consecutive_question_turns": maximum_streak,
        "duplicate_replies": duplicate_replies,
        "replayed_user_messages": replayed_user_messages,
        "leak_matches": leaks,
        "automatic_hard_fail": bool(
            leaks
            or duplicate_replies
            or replayed_user_messages
            or maximum_streak >= 4
        ),
        "human_review_required": [
            "unsupported or hallucinated self-experience",
            "false read/watch/search claim",
            "USER action reused as YUI action",
            "correction response",
            "irrelevant memory use",
            "natural Japanese and relationship register",
        ],
    }


def _write_result(kind: str, payload: dict[str, Any]) -> Path:
    output_dir = ROOT / "logs" / "acceptance"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = SystemClock().now().strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"REAL_{kind.upper()}_{timestamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def run_conversation() -> tuple[dict[str, Any], Path]:
    application = Application.build(_config())
    try:
        manifest = _manifest(application)
        if manifest["dirty"]:
            raise RuntimeError("dirty worktree: real acceptance stopped")
        healthy = await application.llm.health()
        if not healthy:
            raise RuntimeError("Ollama/model health check failed")
        if application.conversation is None:
            raise RuntimeError("conversation service was not assembled")

        call_count_before = application.llm_calls.count()
        turns: list[dict[str, Any]] = []
        for index, prompt in enumerate(CONVERSATION_PROMPTS, start=1):
            message = InboundMessage(
                message_id=f"real-acceptance-in-{index}",
                channel_id=CHANNEL_ID,
                channel_type="direct_message",
                author_id=OWNER_ID,
                text=prompt,
                created_at=SystemClock().now(),
            )
            result = await application.conversation.handle_inbound(message)
            reply = result.outbound.text if result.outbound is not None else None
            if result.should_send:
                await application.conversation.confirm_sent(
                    result,
                    message_id=f"real-acceptance-out-{index}",
                    channel_type="direct_message",
                )
            turn = {
                "turn": index,
                "prompt": prompt,
                "accepted": result.accepted,
                "silent": result.silent,
                "suppressed": result.suppressed,
                "intent": result.intent.intent.value if result.intent else None,
                "reply": reply,
                "retrieval_source": (
                    result.retrieval.relevance_source if result.retrieval else None
                ),
                "recalled_memory_ids": (
                    list(result.retrieval.selected_ids) if result.retrieval else []
                ),
            }
            turns.append(turn)
            print(
                json.dumps(
                    {"turn": index, "prompt": prompt, "reply": reply},
                    ensure_ascii=False,
                ),
                flush=True,
            )

        await application.conversation.drain_background()
        payload = {
            "kind": "real_ollama_conversation",
            "manifest": manifest,
            "ollama_healthy": healthy,
            "llm_calls": application.llm_calls.count() - call_count_before,
            "turns": turns,
            "metrics": conversation_metrics(turns),
            "status": "EVIDENCE_READY_FOR_REVIEW",
        }
        path = _write_result("conversation", payload)
        return payload, path
    finally:
        await application.stop("real conversation acceptance complete")


def _seed_memory(
    application: Application,
    *,
    label: str,
    summary: str,
    topics: tuple[str, ...],
    importance: float,
    accessibility: float,
    age_days: int,
) -> str:
    now = SystemClock().now()
    episode = application.memories.open_episode(
        conversation_id=None,
        origin="simulated_past",
        started_at=now - timedelta(days=age_days),
    )
    application.memories.close_episode(
        episode.episode_id,
        ended_at=now - timedelta(days=age_days) + timedelta(hours=1),
        reason=f"real_acceptance:{label}",
        status="encoded",
    )
    memory_id = ids.new_id(ids.MEMORY)
    inserted = application.memories.insert_memory(
        EpisodicMemory(
            memory_id=memory_id,
            episode_id=episode.episode_id,
            origin="simulated_past",
            summary=summary,
            topics=topics,
            importance=importance,
            emotional_intensity=0.0,
            accessibility=accessibility,
            occurred_at=now - timedelta(days=age_days),
            created_at=now,
            updated_at=now,
        )
    )
    if not inserted:
        raise RuntimeError(f"failed to insert {label} acceptance memory")
    return memory_id


def _memory_state(application: Application, memory_id: str) -> dict[str, Any]:
    memory = application.memories.get_memory(memory_id)
    if memory is None:
        raise RuntimeError(f"missing acceptance memory {memory_id}")
    return {
        "memory_id": memory.memory_id,
        "summary": memory.summary,
        "accessibility": memory.accessibility,
        "recall_count": memory.recall_count,
        "last_recalled_at": (
            memory.last_recalled_at.isoformat() if memory.last_recalled_at else None
        ),
    }


def _report_view(report: Any) -> dict[str, Any]:
    return {
        "group_id": report.group_id,
        "source": report.relevance_source,
        "llm_call_id": report.llm_call_id,
        "candidate_ids": list(report.candidate_ids),
        "passed_ids": list(report.passed_ids),
        "selected_ids": list(report.selected_ids),
        "practised": list(report.practised),
        "judgements": [
            {
                "memory_id": judgement.memory_id,
                "relevance": judgement.relevance,
                "reason": judgement.reason,
                "source": judgement.source,
            }
            for judgement in report.judgements
        ],
        "rejected": [
            {
                "memory_id": item.memory_id,
                "stage": item.stage,
                "reason": item.reason,
                "relevance": item.relevance,
                "availability": item.availability,
            }
            for item in report.rejected
        ],
    }


async def run_memory(turns: int) -> tuple[dict[str, Any], Path]:
    if not 100 <= turns <= 200:
        raise ValueError("memory saturation turns must be between 100 and 200")
    application = Application.build(_config())
    try:
        manifest = _manifest(application)
        if manifest["dirty"]:
            raise RuntimeError("dirty worktree: real acceptance stopped")
        healthy = await application.llm.health()
        if not healthy:
            raise RuntimeError("Ollama/model health check failed")

        call_count_before = application.llm_calls.count()
        irrelevant_id = _seed_memory(
            application,
            label="irrelevant_high",
            summary="去年の夏、港で大きな花火を見た",
            topics=("花火", "港", "夏"),
            importance=0.90,
            accessibility=0.90,
            age_days=365,
        )
        inaccessible_id = _seed_memory(
            application,
            label="relevant_inaccessible",
            summary="古い図書館で初めて詩集を読んだ",
            topics=("詩", "本", "図書館"),
            importance=0.05,
            accessibility=0.01,
            age_days=3650,
        )
        accessible_id = _seed_memory(
            application,
            label="relevant_accessible",
            summary="雨の日に図書館で静かに詩集を読んだ",
            topics=("詩", "本", "図書館", "雨"),
            importance=0.55,
            accessibility=0.55,
            age_days=30,
        )
        ids_by_label = {
            "irrelevant_high": irrelevant_id,
            "relevant_inaccessible": inaccessible_id,
            "relevant_accessible": accessible_id,
        }
        before = {
            label: _memory_state(application, memory_id)
            for label, memory_id in ids_by_label.items()
        }

        query = "昔、図書館で読んだ詩集の記憶を思い出して"
        retrieval_rows_before = int(
            application.db.scalar("SELECT COUNT(*) FROM memory_retrievals") or 0
        )
        inspector = await application.memory_inspector.preview_retrieval(
            query, mode=RecallMode.REFLECTIVE
        )
        after_inspector = {
            label: _memory_state(application, memory_id)
            for label, memory_id in ids_by_label.items()
        }
        retrieval_rows_after = int(
            application.db.scalar("SELECT COUNT(*) FROM memory_retrievals") or 0
        )

        last_report = None
        source_counts: dict[str, int] = {}
        selected_counts = {memory_id: 0 for memory_id in ids_by_label.values()}
        for index in range(1, turns + 1):
            last_report = await application.memory.recall(
                query, mode=RecallMode.REFLECTIVE
            )
            source_counts[last_report.relevance_source] = (
                source_counts.get(last_report.relevance_source, 0) + 1
            )
            for memory_id in last_report.selected_ids:
                if memory_id in selected_counts:
                    selected_counts[memory_id] += 1
            if index == 1 or index % 10 == 0:
                state = _memory_state(application, accessible_id)
                print(
                    json.dumps(
                        {
                            "turn": index,
                            "accessibility": state["accessibility"],
                            "recall_count": state["recall_count"],
                            "source": last_report.relevance_source,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        memory_count_before_spontaneous = int(
            application.db.scalar("SELECT COUNT(*) FROM episodic_memories") or 0
        )
        spontaneous = await application.memory.associate(("図書館", "詩集", "雨"))
        spontaneous_first = application.memory.mark_spontaneously_recalled(spontaneous)
        spontaneous_second = application.memory.mark_spontaneously_recalled(spontaneous)
        memory_count_after_spontaneous = int(
            application.db.scalar("SELECT COUNT(*) FROM episodic_memories") or 0
        )
        spontaneous_rows = [
            dict(row)
            for row in application.memories.retrievals_in_group(spontaneous.group_id)
        ]

        after = {
            label: _memory_state(application, memory_id)
            for label, memory_id in ids_by_label.items()
        }
        checks = {
            "ollama_only_reranking": source_counts == {"llm": turns},
            "irrelevant_high_is_candidate": irrelevant_id in inspector.candidate_ids,
            "irrelevant_high_rejected": irrelevant_id not in inspector.passed_ids,
            "relevant_inaccessible_passed_semantics": inaccessible_id in inspector.passed_ids,
            "relevant_inaccessible_not_selected": inaccessible_id not in inspector.selected_ids,
            "inspector_zero_retrieval_writes": retrieval_rows_before
            == retrieval_rows_after,
            "inspector_zero_memory_side_effect": before == after_inspector,
            "candidate_not_practised_irrelevant": (
                after["irrelevant_high"]["recall_count"]
                == before["irrelevant_high"]["recall_count"]
                and after["irrelevant_high"]["accessibility"]
                == before["irrelevant_high"]["accessibility"]
            ),
            "candidate_not_practised_inaccessible": (
                after["relevant_inaccessible"]["recall_count"]
                == before["relevant_inaccessible"]["recall_count"]
                and after["relevant_inaccessible"]["accessibility"]
                == before["relevant_inaccessible"]["accessibility"]
            ),
            "saturation_exercised": turns >= 100,
            "accessibility_ceiling_respected": all(
                item["accessibility"] <= 0.90 for item in after.values()
            ),
            "relevant_accessible_practised": (
                after["relevant_accessible"]["recall_count"]
                > before["relevant_accessible"]["recall_count"]
            ),
            "spontaneous_selected": bool(spontaneous.selected_ids),
            "spontaneous_practised_once": bool(spontaneous_first)
            and not spontaneous_second,
            "spontaneous_state_recorded": any(
                row["state"] == "spontaneously_recalled" for row in spontaneous_rows
            ),
            "no_recursive_memory_created": memory_count_before_spontaneous
            == memory_count_after_spontaneous,
        }
        payload = {
            "kind": "real_ollama_memory",
            "manifest": manifest,
            "ollama_healthy": healthy,
            "requested_turns": turns,
            "llm_calls": application.llm_calls.count() - call_count_before,
            "memory_ids": ids_by_label,
            "before": before,
            "inspector": _report_view(inspector),
            "last_recall": _report_view(last_report),
            "source_counts": source_counts,
            "selected_counts": selected_counts,
            "spontaneous": {
                "report": _report_view(spontaneous),
                "first_practised": list(spontaneous_first),
                "second_practised": list(spontaneous_second),
                "retrieval_rows": spontaneous_rows,
            },
            "after": after,
            "checks": checks,
            "status": "PASS" if all(checks.values()) else "FAIL",
        }
        path = _write_result("memory", payload)
        return payload, path
    finally:
        await application.stop("real memory acceptance complete")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("conversation", help="run real-Ollama conversation evidence")
    memory = subparsers.add_parser("memory", help="run real-Ollama memory gate")
    memory.add_argument("--turns", type=int, default=100)
    return parser


async def _main() -> int:
    args = _parser().parse_args()
    if args.command == "conversation":
        payload, path = await run_conversation()
    else:
        payload, path = await run_memory(args.turns)
    print(json.dumps({"status": payload["status"], "path": str(path)}, ensure_ascii=False))
    return 0 if payload["status"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

"""INVARIANT: being easy to remember is not the same as being relevant
(rebuild spec 17.3-17.5, Phase 2).

The production database ended with memories pinned near accessibility 1.0 that
had never been the answer to anything. The loop was simple and complete: a
search returned a memory, returning it marked it used, being used practised it,
practice raised accessibility, accessibility raised its blended score, and the
higher score returned it from the next search. Nothing in that circuit asked
whether the memory had anything to do with what the USER had just said.

These are the fifteen tests Phase 2 names, in order, plus what they need to
mean anything.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.memory.candidates import CandidateGenerator, bridge_topics
from app.memory.engine import MemoryEngine
from app.memory.material import EpisodeMaterial, StaticMaterialSource
from app.memory.models import EpisodicMemory
from app.memory.policy import MemoryPolicy
from app.memory.recall_mode import RecallMode, classify
from app.memory.recall_models import RelevanceJudgement
from app.memory.relevance import SemanticReranker, fallback_judgements
from app.memory.inspector import MemoryInspector
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMResponse
from app.storage.repositories.memory import MemoryRepository
from tests.support import relevance_answer
from tests.unit.test_memory import memories, memory_policy  # noqa: F401

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


# --- a model that judges what it is actually shown ---------------------------


class RelevanceClient:
    """A model that answers the real rerank prompt.

    It reads the candidate ids out of the rendered prompt and labels them, so
    the whole Stage 2 path — prompt rendering, schema validation, id matching —
    is exercised rather than stubbed past.
    """

    model = "test-model"

    def __init__(self, *, labels: dict[str, str] | None = None, default: str = "irrelevant") -> None:
        self.labels = labels or {}
        self.default = default
        self.prompts: list[str] = []
        self.failure: Exception | None = None

    async def generate(self, request):
        prompt = request.messages[0].content
        self.prompts.append(prompt)
        if self.failure is not None:
            raise self.failure
        import json
        import re

        ids = re.findall(r"memory_id: (\S+)", prompt)
        payload = {
            "judgements": [
                {
                    "memory_id": memory_id,
                    "relevance": self.labels.get(memory_id, self.default),
                    "reason": "テスト",
                }
                for memory_id in ids
            ]
        }
        return LLMResponse(
            text=json.dumps(payload, ensure_ascii=False),
            model=self.model,
            created_at=NOW,
            latency_ms=5,
        )

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def store(
    repository: MemoryRepository,
    *,
    summary: str,
    topics: tuple[str, ...] = (),
    accessibility: float = 0.5,
    importance: float = 0.5,
    occurred_at: datetime | None = None,
    memory_id: str | None = None,
    origin: str = "real_discord",
    content_confidence: float = 0.8,
    temporal_confidence: float = 0.8,
) -> EpisodicMemory:
    episode = repository.open_episode(
        conversation_id=None,
        origin=origin,
        started_at=occurred_at or NOW,
    )
    repository.close_episode(
        episode.episode_id, ended_at=occurred_at or NOW, reason="test"
    )
    memory = EpisodicMemory(
        memory_id=memory_id or f"mem_{abs(hash(summary)) % 10**9}",
        episode_id=episode.episode_id,
        origin=origin,  # type: ignore[arg-type]
        summary=summary,
        topics=topics,
        importance=importance,
        emotional_intensity=0.3,
        accessibility=accessibility,
        content_confidence=content_confidence,
        source_confidence=0.9,
        temporal_confidence=temporal_confidence,
        novelty=0.4,
        prediction_error=0.1,
        occurred_at=occurred_at or NOW,
        created_at=occurred_at or NOW,
        updated_at=occurred_at or NOW,
        last_decayed_at=occurred_at or NOW,
    )
    repository.insert_memory(memory)
    return memory


@pytest.fixture
def engine_for(memories, memory_policy, prompt_registry, clock):  # noqa: F811
    def build(client: RelevanceClient) -> MemoryEngine:
        generator = StructuredGenerator(
            client, prompts=prompt_registry, clock=clock, max_attempts=1
        )
        return MemoryEngine(
            repository=memories,
            policy=memory_policy,
            structured=generator,
            prompts=prompt_registry,
            material=StaticMaterialSource(EpisodeMaterial(text="", turn_count=0)),
            reranker=SemanticReranker(structured=generator, prompts=prompt_registry),
            clock=clock,
        )

    return build


# --- 1. an irrelevant memory does not surface however well remembered --------


async def test_a_high_accessibility_irrelevant_memory_is_not_recalled(
    engine_for, memories  # noqa: F811
) -> None:
    """§2F: semantic relevance < threshold なら accessibility が 1.0 でも Recall 禁止."""
    pinned = store(
        memories,
        summary="祖母の家で花火を見た",
        topics=("花火",),
        accessibility=1.0,
        importance=1.0,
    )
    engine = engine_for(RelevanceClient(default="irrelevant"))

    report = await engine.recall("プログラミングって難しいね", now=NOW)

    assert report.selected == ()
    assert pinned.memory_id not in report.selected_ids
    # And it was rejected *for being irrelevant*, not for being unreachable.
    if pinned.memory_id in report.candidate_ids:
        rejection = next(
            item for item in report.rejected if item.memory_id == pinned.memory_id
        )
        assert rejection.stage == "relevance"


# --- 2. relevant but not reachable ------------------------------------------


async def test_a_relevant_but_faded_memory_can_fail_to_come_to_mind(
    engine_for, memories  # noqa: F811
) -> None:
    """関係あるが、いまは思い出しにくい — a state the system must be able to be in."""
    faded = store(
        memories,
        summary="海に行った話をした",
        topics=("海",),
        accessibility=0.02,
        importance=0.05,
        occurred_at=NOW - timedelta(days=400),
    )
    engine = engine_for(RelevanceClient(default="strong"))

    report = await engine.recall("海の話", now=NOW)

    assert faded.memory_id in report.candidate_ids
    assert faded.memory_id in report.passed_ids  # relevance said yes
    assert faded.memory_id not in report.selected_ids  # availability said no
    rejection = next(
        item for item in report.rejected if item.memory_id == faded.memory_id
    )
    assert rejection.stage == "availability"


# --- 3. being a candidate changes nothing ------------------------------------


async def test_becoming_a_candidate_does_not_touch_accessibility(
    engine_for, memories  # noqa: F811
) -> None:
    """§2C: 「検索された」と「思い出した」は違う."""
    memory = store(memories, summary="海に行った話をした", topics=("海",), accessibility=0.5)
    engine = engine_for(RelevanceClient(default="irrelevant"))

    before = memories.get_memory(memory.memory_id)
    report = await engine.recall("海の話", now=NOW)
    after = memories.get_memory(memory.memory_id)

    assert memory.memory_id in report.candidate_ids
    assert after.accessibility == before.accessibility
    assert after.recall_count == before.recall_count
    assert after.last_recalled_at == before.last_recalled_at


# --- 4. only what was actually remembered practises --------------------------


async def test_only_a_recalled_memory_practises(engine_for, memories) -> None:  # noqa: F811
    """§2K: practice は consciously_recalled か used_in_reply のときだけ."""
    memory = store(memories, summary="海に行った話をした", topics=("海",), accessibility=0.5)
    engine = engine_for(RelevanceClient(default="strong"))

    # Ordinary conversation: the memory reaches context, which is availability,
    # not recollection. Nothing practises yet.
    report = await engine.recall("海の話", now=NOW)
    assert report.selected_ids == (memory.memory_id,)
    assert memories.get_memory(memory.memory_id).accessibility == 0.5

    # The reply actually rests on it. Now it practises.
    used = engine.mark_used_in_reply(report, "海に行った話、よかったね。", now=NOW)
    assert used == (memory.memory_id,)
    assert memories.get_memory(memory.memory_id).accessibility > 0.5


async def test_a_memory_the_reply_ignored_does_not_practise(
    engine_for, memories  # noqa: F811
) -> None:
    memory = store(memories, summary="海に行った話をした", topics=("海",), accessibility=0.5)
    engine = engine_for(RelevanceClient(default="strong"))
    report = await engine.recall("海の話", now=NOW)

    used = engine.mark_used_in_reply(report, "そうだね。", now=NOW)

    assert used == ()
    assert memories.get_memory(memory.memory_id).accessibility == 0.5


async def test_a_deliberate_lookup_is_a_conscious_recall(
    engine_for, memories  # noqa: F811
) -> None:
    """Being asked her age and answering *is* remembering, reply or no reply."""
    store(memories, summary="2007年に生まれた", topics=("誕生", "生まれ"), accessibility=0.6)
    engine = engine_for(RelevanceClient(default="strong"))

    report = await engine.recall("ゆいは何歳？", now=NOW)

    assert report.mode is RecallMode.AUTOBIOGRAPHICAL_FACT
    assert report.practised == report.selected_ids
    assert report.practised


# --- 5. diminishing returns, counting practice only --------------------------


async def test_repeated_practice_diminishes(engine_for, memories) -> None:  # noqa: F811
    """§2L, and the half of it that was wrong before: candidate lookups are not
    repetitions, so they must not count towards the diminishing window."""
    memory = store(memories, summary="海に行った話をした", topics=("海",), accessibility=0.3)
    engine = engine_for(RelevanceClient(default="strong"))

    gains = []
    for _ in range(4):
        report = await engine.recall("海の話", mode=RecallMode.REFLECTIVE, now=NOW)
        assert report.selected
        after = memories.get_memory(memory.memory_id).accessibility
        gains.append(after)

    steps = [second - first for first, second in zip(gains, gains[1:])]
    assert steps[0] > steps[-1]


async def test_a_candidate_lookup_is_not_a_repetition(
    engine_for, memories  # noqa: F811
) -> None:
    memory = store(memories, summary="海に行った話をした", topics=("海",), accessibility=0.3)
    engine = engine_for(RelevanceClient(default="irrelevant"))

    for _ in range(5):
        await engine.recall("海の話", now=NOW)

    window = NOW - timedelta(hours=48)
    assert memories.practices_since(memory.memory_id, window) == 0


# --- 6. a debug preview changes nothing --------------------------------------


async def test_a_debug_preview_changes_nothing(
    engine_for, memories, clock  # noqa: F811
) -> None:
    """§2Q. The inspector is a different object with no writer at all."""
    memory = store(memories, summary="海に行った話をした", topics=("海",), accessibility=0.5)
    engine = engine_for(RelevanceClient(default="strong"))
    inspector = MemoryInspector(engine.retriever, clock=clock)

    before = memories.get_memory(memory.memory_id)
    report = await inspector.preview_retrieval("海の話", now=NOW)
    after = memories.get_memory(memory.memory_id)

    assert report.selected_ids == (memory.memory_id,)
    assert after == before
    assert memories.retrievals_in_group(report.group_id) == []


async def test_ten_debug_searches_leave_accessibility_untouched(
    engine_for, memories, clock  # noqa: F811
) -> None:
    """Test 15. Ten previews, no drift anywhere."""
    memory = store(memories, summary="海に行った話をした", topics=("海",), accessibility=0.5)
    engine = engine_for(RelevanceClient(default="strong"))
    inspector = MemoryInspector(engine.retriever, clock=clock)

    before = memories.get_memory(memory.memory_id)
    for _ in range(10):
        await inspector.preview_retrieval("海の話", now=NOW)
    after = memories.get_memory(memory.memory_id)

    assert after.accessibility == before.accessibility
    assert after.recall_count == before.recall_count
    assert after.updated_at == before.updated_at


def test_the_inspector_holds_no_writer() -> None:
    """Structural, not behavioural: a ``practise=False`` flag would be one
    future line away from being wrong. This has nowhere to write."""
    import inspect

    source = inspect.getsource(MemoryInspector)
    for writer in ("update_accessibility", "record_retrieval", "promote_retrieval", "_practise"):
        assert writer not in source


# --- 7-8. the autobiographical bridge, and its limits ------------------------


async def test_an_age_question_reaches_the_birth_memory(
    engine_for, memories  # noqa: F811
) -> None:
    """Test 7, and §2G: the bridge survives, as a way *in*."""
    birth = store(
        memories,
        summary="2007年の春に生まれた家のこと",
        topics=("誕生", "生まれ", "家"),
        accessibility=0.4,
    )
    engine = engine_for(RelevanceClient(default="strong"))

    report = await engine.recall("ゆいは何歳？", now=NOW)

    assert birth.memory_id in report.candidate_ids
    reasons = next(
        item.reasons for item in report.candidates if item.memory_id == birth.memory_id
    )
    assert "autobiographical_bridge" in reasons


async def test_an_ordinary_short_message_does_not_surface_the_birth_memory(
    engine_for, memories  # noqa: F811
) -> None:
    """Test 8. The bridge must not fire on everything, and even when a memory
    does reach Stage 2 the gate still has to pass it."""
    store(
        memories,
        summary="2007年の春に生まれた家のこと",
        topics=("誕生", "生まれ", "家"),
        accessibility=1.0,
    )
    engine = engine_for(RelevanceClient(default="irrelevant"))

    report = await engine.recall("うん、そうだね", now=NOW)

    assert report.selected == ()


def test_the_bridge_is_only_a_candidate_source() -> None:
    """§2G: its role is 'help candidate generation', not 'confirm recall'."""
    assert bridge_topics("ゆいは何歳？")
    assert bridge_topics("最初の記憶は？")
    assert bridge_topics("今日はいい天気だね") == ()


# --- 9. the objective archive is not a fallback ------------------------------


def _retrieval_code(module: str) -> ast.Module:
    return ast.parse((REPO_ROOT / "app" / "memory" / module).read_text(encoding="utf-8"))


RETRIEVAL_MODULES = ("retrieval.py", "candidates.py", "selection.py", "relevance.py")


def test_retrieval_cannot_reach_the_archive() -> None:
    """Test 9, §2H. Structural: there is no import through which it could.

    Parsed rather than grepped — the prose in these modules names the archive
    tables precisely because it is explaining why they are off limits, and a
    text search would be satisfied by deleting the explanation.
    """
    for module in RETRIEVAL_MODULES:
        tree = _retrieval_code(module)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        assert not any(name.startswith("app.events") for name in imported), module

        names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        assert "EventStore" not in names, module
        assert not (names & {"life_months", "life_years", "event_store"}), module

        # SQL anywhere outside the storage layer is caught by
        # test_sql_lives_only_in_the_storage_layer; what matters here is that
        # these modules cannot name the archive at all.


async def test_nothing_encoded_means_nothing_recalled(engine_for) -> None:
    engine = engine_for(RelevanceClient(default="strong"))
    report = await engine.recall("海に行った話", now=NOW)
    assert report.selected == ()
    assert report.candidates == ()


# --- 10. a broken reranker recalls nothing, never anything -------------------


async def test_a_failed_rerank_does_not_fall_back_to_accessibility(
    engine_for, memories  # noqa: F811
) -> None:
    """Failure policy. LLM が壊れたから accessibility 順で何でも出す、は禁止."""
    pinned = store(
        memories,
        summary="祖母の家で花火を見た",
        topics=("花火",),
        accessibility=1.0,
        importance=1.0,
    )
    client = RelevanceClient(default="strong")
    client.failure = RuntimeError("ollama is down")
    engine = engine_for(client)

    # Reflective mode lets importance raise it into the pool, so it really is a
    # candidate when the reranker dies — otherwise this would prove nothing but
    # that the query did not match.
    report = await engine.recall(
        "プログラミングの話", mode=RecallMode.REFLECTIVE, now=NOW
    )

    assert pinned.memory_id in report.candidate_ids
    assert report.relevance_source == "fallback"
    assert pinned.memory_id not in report.selected_ids
    assert pinned.memory_id not in report.passed_ids


async def test_the_fallback_still_passes_hard_lexical_evidence(
    engine_for, memories  # noqa: F811
) -> None:
    """Conservative is not the same as useless: a memory that plainly shares
    the query's content still gets through when the model is unavailable."""
    memory = store(memories, summary="海に行った話をした", topics=("海",), accessibility=0.7)
    client = RelevanceClient(default="strong")
    client.failure = RuntimeError("ollama is down")
    engine = engine_for(client)

    report = await engine.recall("海に行った話", now=NOW)

    assert report.relevance_source == "fallback"
    assert memory.memory_id in report.selected_ids


def test_the_fallback_never_invents_a_strong_judgement() -> None:
    from app.memory.recall_models import Candidate

    memory = EpisodicMemory(
        memory_id="mem_1",
        episode_id="epi_1",
        origin="real_discord",
        summary="海に行った話をした",
        topics=("海",),
        importance=0.5,
        accessibility=0.9,
        occurred_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    judgements = fallback_judgements("海に行った話", (Candidate(memory=memory),))
    assert judgements[0].source == "fallback"
    assert judgements[0].relevance in ("relevant", "irrelevant")


# --- 11. zero recalls flows through normally ---------------------------------


async def test_zero_recalls_is_a_normal_result(engine_for, memories) -> None:  # noqa: F811
    """§2I: 「何も思い出さない」は正常結果である."""
    store(memories, summary="祖母の家で花火を見た", topics=("花火",), accessibility=0.9)
    engine = engine_for(RelevanceClient(default="irrelevant"))

    report = await engine.recall("うん", now=NOW)

    assert report.is_empty
    assert report.selected == ()
    # And it is a report, not an error: the conversation path can read it.
    assert report.mode is RecallMode.CONVERSATIONAL
    assert report.group_id


# --- 12. the record survives a restart ---------------------------------------


async def test_the_retrieval_record_survives_a_restart(
    engine_for, memories, db  # noqa: F811
) -> None:
    """Test 12. What was practised, and why, is on disk rather than in a process."""
    memory = store(memories, summary="海に行った話をした", topics=("海",), accessibility=0.5)
    engine = engine_for(RelevanceClient(default="strong"))
    report = await engine.recall("海の話", mode=RecallMode.REFLECTIVE, now=NOW)
    assert report.practised

    reopened = MemoryRepository(db)
    rows = reopened.retrievals_in_group(report.group_id)
    assert rows
    chosen = [row for row in rows if row["memory_id"] == memory.memory_id]
    assert chosen[0]["state"] == "consciously_recalled"
    assert chosen[0]["practice_applied"] == 1
    assert reopened.get_memory(memory.memory_id).recall_count == 1


# --- 13. suppressed and invalidated memories are gone from every mode --------


async def test_a_suppressed_memory_is_unreachable_in_every_mode(
    engine_for, memories  # noqa: F811
) -> None:
    memory = store(memories, summary="海に行った話をした", topics=("海",), accessibility=0.9)
    engine = engine_for(RelevanceClient(default="strong"))
    engine.suppress(memory.memory_id)

    for mode in RecallMode:
        report = await engine.recall("海の話", mode=mode, now=NOW)
        assert report.candidates == (), mode
        assert report.selected == (), mode


async def test_an_invalidated_memory_is_unreachable_in_every_mode(
    engine_for, memories  # noqa: F811
) -> None:
    memory = store(memories, summary="海に行った話をした", topics=("海",), accessibility=0.9)
    engine = engine_for(RelevanceClient(default="strong"))
    engine.invalidate(memory.memory_id)

    for mode in RecallMode:
        report = await engine.recall("海の話", mode=mode, now=NOW)
        assert report.selected == (), mode


# --- 14. the diary is not a memory substitute --------------------------------


def test_only_diary_reading_mode_is_about_the_diary() -> None:
    """Test 14. Until Phase 10 there is no diary at all, so what is held here
    is the boundary: reading the diary is its own mode, and no other mode may
    quietly become one."""
    assert classify("日記を読み返してみて") is RecallMode.DIARY_READING
    assert classify("今日どうだった？") is not RecallMode.DIARY_READING
    assert classify("ゆいは何歳？") is not RecallMode.DIARY_READING
    assert RecallMode.DIARY_READING.is_deliberate


def test_diary_entries_are_not_an_evidence_kind_for_ordinary_recall() -> None:
    """Retrieval reads episodic and semantic memory. There is no diary table
    to reach yet, and no path in retrieval that would reach one if there were.
    """
    for module in RETRIEVAL_MODULES:
        names = {
            node.attr
            for node in ast.walk(_retrieval_code(module))
            if isinstance(node, ast.Attribute)
        }
        assert not any("diary" in name.lower() for name in names), module


# --- modes -------------------------------------------------------------------


def test_modes_are_classified_from_the_question() -> None:
    assert classify("ゆいは何歳？") is RecallMode.AUTOBIOGRAPHICAL_FACT
    assert classify("生まれはどこ？") is RecallMode.AUTOBIOGRAPHICAL_FACT
    assert classify("今までで一番印象に残ってることは？") is RecallMode.REFLECTIVE
    assert classify("そっか") is RecallMode.CONVERSATIONAL
    assert classify("") is RecallMode.CONVERSATIONAL


def test_only_deliberate_modes_count_as_recollection() -> None:
    assert RecallMode.AUTOBIOGRAPHICAL_FACT.is_deliberate
    assert RecallMode.REFLECTIVE.is_deliberate
    assert not RecallMode.CONVERSATIONAL.is_deliberate
    assert not RecallMode.ASSOCIATIVE.is_deliberate
    assert not RecallMode.CORRECTION_CHECK.is_deliberate


async def test_the_mode_changes_how_wide_stage_one_looks(
    memories, memory_policy  # noqa: F811
) -> None:
    """§2D. Ordinary conversation does not sweep in recent memories; reflection
    does, because 「一番印象に残っているのは」 is a question about importance."""
    for index in range(5):
        store(
            memories,
            summary=f"なんでもない日のこと{index}",
            topics=("日常",),
            importance=0.9,
            memory_id=f"mem_plain_{index}",
        )
    generator = CandidateGenerator(memories, memory_policy.retrieval.candidates)

    ordinary = generator.generate(
        "ぜんぜん関係ない話", mode=RecallMode.CONVERSATIONAL, now=NOW
    )
    reflective = generator.generate(
        "ぜんぜん関係ない話", mode=RecallMode.REFLECTIVE, now=NOW
    )

    assert ordinary == ()
    assert reflective


# --- 2N: the associative API -------------------------------------------------


async def test_associate_recalls_from_cues(engine_for, memories) -> None:  # noqa: F811
    """§2N. Nothing drives this yet; the API exists so that when the Autonomous
    Runtime does, it goes through the same four stages rather than growing a
    second, simpler retrieval path beside them."""
    memory = store(memories, summary="祖母の家で花火を見た", topics=("花火",), accessibility=0.9)
    engine = engine_for(RelevanceClient(default="strong"))

    report = await engine.associate(("花火", "夏"), now=NOW)

    assert report.mode is RecallMode.ASSOCIATIVE
    assert memory.memory_id in report.candidate_ids
    reasons = next(
        item.reasons for item in report.candidates if item.memory_id == memory.memory_id
    )
    assert "cue" in reasons


# --- 2O: confidence travels with the memory ----------------------------------


async def test_an_uncertain_memory_is_offered_with_a_hedge(
    engine_for, memories  # noqa: F811
) -> None:
    """§2O: 低 confidence なのに断言させない."""
    store(
        memories,
        summary="中学生のころの話",
        topics=("学校",),
        accessibility=0.9,
        content_confidence=0.35,
        temporal_confidence=0.3,
    )
    engine = engine_for(RelevanceClient(default="strong"))

    report = await engine.recall("中学生のころの話", now=NOW)

    assert report.selected
    line = report.selected[0].as_context_line()
    assert "はっきりは覚えていない" in line


async def test_a_confident_memory_is_offered_plainly(
    engine_for, memories  # noqa: F811
) -> None:
    store(
        memories,
        summary="海に行った話をした",
        topics=("海",),
        accessibility=0.9,
        content_confidence=0.9,
        temporal_confidence=0.9,
    )
    engine = engine_for(RelevanceClient(default="strong"))

    report = await engine.recall("海の話", now=NOW)

    assert report.selected[0].as_context_line() == "- 海に行った話をした"


# --- 2P: recall is not reconstruction ----------------------------------------


async def test_recall_never_rewrites_what_is_remembered(
    engine_for, memories  # noqa: F811
) -> None:
    """§2P: Recall ≠ Reconstruction."""
    memory = store(memories, summary="海に行った話をした", topics=("海",), accessibility=0.8)
    engine = engine_for(RelevanceClient(default="strong"))

    report = await engine.recall("海の話", mode=RecallMode.REFLECTIVE, now=NOW)
    engine.mark_used_in_reply(report, "海に行った話、よかったね。", now=NOW)

    after = memories.get_memory(memory.memory_id)
    assert after.summary == memory.summary
    assert after.revision_count == 0
    assert memories.revisions(memory.memory_id) == []


# --- observability -----------------------------------------------------------


async def test_every_candidate_leaves_a_row_explaining_itself(
    engine_for, memories  # noqa: F811
) -> None:
    """Observability. Without this Phase 2 lands where it started: memory works
    and nobody can say why that memory and not another one."""
    kept = store(memories, summary="海に行った話をした", topics=("海",), accessibility=0.8,
                 memory_id="mem_kept")
    dropped = store(memories, summary="海辺で本を読んだ", topics=("海",), accessibility=0.8,
                    memory_id="mem_dropped")
    engine = engine_for(
        RelevanceClient(labels={kept.memory_id: "strong", dropped.memory_id: "irrelevant"})
    )

    report = await engine.recall("海の話", now=NOW)
    rows = {row["memory_id"]: row for row in memories.retrievals_in_group(report.group_id)}

    assert set(rows) == set(report.candidate_ids)
    assert rows[kept.memory_id]["state"] == "selected"
    assert rows[kept.memory_id]["relevance"] == "strong"
    assert rows[kept.memory_id]["availability"] is not None
    assert rows[dropped.memory_id]["state"] == "candidate"
    assert rows[dropped.memory_id]["relevance"] == "irrelevant"
    assert rows[dropped.memory_id]["reject_stage"] == "relevance"
    for row in rows.values():
        assert row["mode"] == "CONVERSATIONAL"
        assert row["accessibility_at"] is not None
        assert row["candidate_reasons"]
        assert row["relevance_source"] == "llm"


async def test_the_inspector_explains_why_a_memory_did_not_come_up(
    engine_for, memories, clock  # noqa: F811
) -> None:
    """The debug inspector's actual output (§Debug Inspector)."""
    store(memories, summary="海に行った話をした", topics=("海",), accessibility=0.21)
    engine = engine_for(RelevanceClient(default="strong"))
    inspector = MemoryInspector(engine.retriever, clock=clock)

    text = await inspector.describe("海の話", now=NOW)

    assert "semantic relevance: strong" in text
    assert "accessibility: 0.21" in text
    assert "practice applied: no" in text
    assert "selected:" in text


# --- the judgement type ------------------------------------------------------


def test_weak_does_not_pass_the_gate_by_default() -> None:
    """§2E: irrelevant → reject, weak → 原則 reject."""
    assert not RelevanceJudgement(memory_id="m", relevance="irrelevant").passed
    assert not RelevanceJudgement(memory_id="m", relevance="weak").passed
    assert RelevanceJudgement(memory_id="m", relevance="relevant").passed
    assert RelevanceJudgement(memory_id="m", relevance="strong").passed


async def test_a_candidate_the_model_ignored_does_not_pass(
    engine_for, memories  # noqa: F811
) -> None:
    """Silence is not assent (§4.6 silent pass 禁止)."""
    store(memories, summary="海に行った話をした", topics=("海",), accessibility=0.9)
    client = RelevanceClient(default="strong")

    async def empty(request):
        client.prompts.append(request.messages[0].content)
        return LLMResponse(
            text='{"judgements": []}', model="test-model", created_at=NOW, latency_ms=1
        )

    client.generate = empty  # type: ignore[method-assign]
    engine = engine_for(client)

    report = await engine.recall("海の話", now=NOW)

    assert report.candidates
    assert report.selected == ()


def test_the_reranker_answer_helper_matches_the_prompt() -> None:
    """The scripted-model helper reads ids from the rendered prompt, so a test
    using it cannot accidentally judge a memory that was never offered."""
    prompt = "- memory_id: mem_a\n  内容: x\n- memory_id: mem_b\n  内容: y"
    answer = relevance_answer(prompt, "weak")
    assert "mem_a" in answer and "mem_b" in answer and "weak" in answer

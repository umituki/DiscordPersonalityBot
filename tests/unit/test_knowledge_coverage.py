"""Where period knowledge comes from, and how much of it (patch spec 16).

The 2026-08-02 Genesis had zero sources and zero candidates: the builder was
there, the funnel was there, and nothing ever produced anything for either of
them to work on. Two things are added — providers, and a plan — and both are
constrained: the authority is an external source, never what a model happens to
know, and a window that produced nothing is recorded as a gap rather than
passed over.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.knowledge.builder import Candidate, KnowledgeBuilder
from app.knowledge.coverage import CoveragePlanner
from app.knowledge.models import COVERAGE_CLASSES
from app.knowledge.policy import KnowledgePolicy
from app.knowledge.providers import (
    BundleProvider,
    CoverageRequest,
    ProviderError,
    ProviderRegistry,
    ToolKnowledgeProvider,
)
from app.storage.repositories.knowledge import CoverageJobRepository, KnowledgeRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_DIR = REPO_ROOT / "config" / "knowledge"

PERIOD_START = datetime(2003, 1, 1, tzinfo=timezone.utc)
PERIOD_END = datetime(2006, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def knowledge_policy() -> KnowledgePolicy:
    return KnowledgePolicy.load(REPO_ROOT / "config" / "policies" / "knowledge.yaml")


@pytest.fixture
def builder(db, knowledge_policy, clock) -> KnowledgeBuilder:
    return KnowledgeBuilder(
        knowledge=KnowledgeRepository(db),
        jobs=CoverageJobRepository(db),
        policy=knowledge_policy,
        clock=clock,
    )


@pytest.fixture
def registry(builder) -> ProviderRegistry:
    return ProviderRegistry(builder=builder)


def request_for(coverage_class: str, **kwargs) -> CoverageRequest:
    defaults = dict(period_start=PERIOD_START, period_end=PERIOD_END)
    defaults.update(kwargs)
    return CoverageRequest(coverage_class=coverage_class, **defaults)  # type: ignore[arg-type]


# --- 16.2 the shipped bundle provider ---------------------------------------
def test_the_shipped_bundle_loads() -> None:
    entries = BundleProvider(BUNDLE_DIR).entries()
    assert entries
    assert {entry.coverage_class for entry in entries} == set(COVERAGE_CLASSES)


def test_every_entry_carries_when_it_became_public() -> None:
    """Spec 21.3: a claim without ``available_from`` cannot be checked."""
    for entry in BundleProvider(BUNDLE_DIR).entries():
        assert entry.available_from.tzinfo is not None


def test_nothing_from_after_the_window_is_offered() -> None:
    """The only leak direction that matters: knowledge that did not exist yet."""
    provider = BundleProvider(BUNDLE_DIR)
    window_end = datetime(1980, 1, 1, tzinfo=timezone.utc)
    offered = provider.candidates(
        request_for(
            "environmental",
            period_start=datetime(1970, 1, 1, tzinfo=timezone.utc),
            period_end=window_end,
        )
    )

    assert all(candidate.available_from < window_end for candidate in offered)


def test_long_standing_knowledge_is_offered_to_a_later_window() -> None:
    """Most of what anyone knows predates the period they are living through."""
    provider = BundleProvider(BUNDLE_DIR)
    offered = provider.candidates(
        request_for(
            "foundational",
            period_start=datetime(2003, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2004, 1, 1, tzinfo=timezone.utc),
        )
    )

    assert offered
    assert all(
        candidate.available_from < datetime(2003, 1, 1, tzinfo=timezone.utc)
        for candidate in offered
    )


def test_knowledge_that_stopped_being_current_is_not_offered_later() -> None:
    provider = BundleProvider(BUNDLE_DIR)
    offered = provider.candidates(
        request_for(
            "environmental",
            period_start=datetime(2020, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2021, 1, 1, tzinfo=timezone.utc),
        )
    )

    assert all(
        candidate.available_until is None
        or candidate.available_until > datetime(2020, 1, 1, tzinfo=timezone.utc)
        for candidate in offered
    )


def test_interest_driven_coverage_follows_the_interests() -> None:
    provider = BundleProvider(BUNDLE_DIR)
    musical = provider.candidates(
        request_for(
            "interest_driven",
            period_start=datetime(1900, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2020, 1, 1, tzinfo=timezone.utc),
            topics=("音楽",),
        )
    )
    assert musical
    assert all(candidate.topic == "音楽" for candidate in musical)


def test_a_missing_directory_is_empty_not_an_error(tmp_path) -> None:
    assert BundleProvider(tmp_path / "nothing").entries() == []


def test_a_malformed_entry_is_refused(tmp_path) -> None:
    """A base that silently drops half its rows is worse than one that refuses."""
    (tmp_path / "bad.yaml").write_text(
        "knowledge:\n  - statement: x\n    coverage_class: nonsense\n"
        '    available_from: "2000-01-01"\n',
        encoding="utf-8",
    )
    with pytest.raises(ProviderError):
        BundleProvider(tmp_path).entries()


def test_an_entry_without_a_date_is_refused(tmp_path) -> None:
    (tmp_path / "bad.yaml").write_text(
        "knowledge:\n  - statement: x\n    coverage_class: foundational\n", encoding="utf-8"
    )
    with pytest.raises(ProviderError):
        BundleProvider(tmp_path).entries()


# --- 16.2 provenance --------------------------------------------------------
def test_a_provider_is_registered_as_a_source(registry, db) -> None:
    source_id = registry.register(BundleProvider(BUNDLE_DIR))

    assert source_id
    assert KnowledgeRepository(db).source(source_id) is not None


def test_candidates_carry_the_source_that_attested_them(registry) -> None:
    registry.register(BundleProvider(BUNDLE_DIR))
    candidates = registry.candidates_for(
        request_for(
            "foundational",
            period_start=datetime(1900, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
    )

    assert candidates
    assert all(candidate.source_id for candidate in candidates)


def test_the_same_claim_twice_is_one_claim(registry) -> None:
    """Patch spec 16.4: ``有名だっただけで取得扱いにしない`` starts here.

    The same statement offered by two providers is one statement, not two
    pieces of corroborating evidence.
    """
    registry.register(BundleProvider(BUNDLE_DIR))
    registry.register(BundleProvider(BUNDLE_DIR, name="second_copy"))

    candidates = registry.candidates_for(
        request_for(
            "foundational",
            period_start=datetime(1900, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
    )

    statements = [candidate.statement for candidate in candidates]
    assert len(statements) == len(set(statements))


def test_one_failing_provider_does_not_end_the_run(registry) -> None:
    class Broken:
        name = "broken"
        kind = "bundle"
        reliability = 0.1

        def candidates(self, request):
            raise RuntimeError("the source is unreachable")

    registry.register(Broken())
    registry.register(BundleProvider(BUNDLE_DIR))

    assert registry.candidates_for(
        request_for(
            "foundational",
            period_start=datetime(1900, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
    )


# --- 16.2 the tool-backed provider ------------------------------------------
class FakeToolResult:
    def __init__(self, success: bool, output=None) -> None:
        self.success = success
        self.output = output


class FakeTools:
    def __init__(self, result) -> None:
        self._result = result
        self.calls: list[tuple] = []

    def call_sync(self, name, arguments):
        self.calls.append((name, arguments))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_a_tool_result_is_used_only_when_the_manager_reports_success() -> None:
    """Spec 26: an unsuccessful tool call yields nothing, never a guess."""
    row = {"statement": "x", "available_from": "2003-01-01"}
    ok = ToolKnowledgeProvider(
        FakeTools(FakeToolResult(True, {"knowledge": [row]})), tool_name="history"
    )
    refused = ToolKnowledgeProvider(
        FakeTools(FakeToolResult(False, {"knowledge": [row]})), tool_name="history"
    )

    assert len(ok.candidates(request_for("historical_cultural"))) == 1
    assert list(refused.candidates(request_for("historical_cultural"))) == []


def test_a_failing_tool_is_a_gap_not_a_crash() -> None:
    provider = ToolKnowledgeProvider(
        FakeTools(RuntimeError("network is gone")), tool_name="history"
    )
    assert list(provider.candidates(request_for("historical_cultural"))) == []


def test_a_tool_manager_without_a_sync_call_yields_nothing() -> None:
    provider = ToolKnowledgeProvider(object(), tool_name="history")
    assert list(provider.candidates(request_for("historical_cultural"))) == []


# --- 16.3 coverage planning -------------------------------------------------
def test_the_plan_covers_every_class_in_every_window(builder, registry) -> None:
    planner = CoveragePlanner(builder=builder, providers=registry, window_days=365)

    requests = planner.plan(period_start=PERIOD_START, period_end=PERIOD_END)

    assert len(requests) == 3 * len(COVERAGE_CLASSES)
    assert {request.coverage_class for request in requests} == set(COVERAGE_CLASSES)
    assert min(request.period_start for request in requests) == PERIOD_START
    assert max(request.period_end for request in requests) == PERIOD_END


def test_the_plan_leaves_no_gap_between_windows(builder, registry) -> None:
    planner = CoveragePlanner(builder=builder, providers=registry, window_days=200)
    windows = sorted(
        {
            (request.period_start, request.period_end)
            for request in planner.plan(period_start=PERIOD_START, period_end=PERIOD_END)
        }
    )

    for (_, first_end), (second_start, _) in zip(windows, windows[1:], strict=False):
        assert first_end == second_start


def test_running_the_plan_stores_knowledge(builder, registry, db) -> None:
    registry.register(BundleProvider(BUNDLE_DIR))
    planner = CoveragePlanner(builder=builder, providers=registry, window_days=365)

    report = planner.run(
        period_start=datetime(1990, 1, 1, tzinfo=timezone.utc),
        period_end=PERIOD_END,
        topics=("音楽",),
    )

    assert report.candidates_stored > 0
    assert KnowledgeRepository(db).count() == report.candidates_stored


def test_an_empty_window_is_recorded_as_a_gap(builder, registry) -> None:
    """Patch spec 16.1: ``0件silent pass禁止``."""
    planner = CoveragePlanner(builder=builder, providers=registry, window_days=365)

    report = planner.run(period_start=PERIOD_START, period_end=PERIOD_END)

    assert report.requests > 0
    assert report.candidates_stored == 0
    assert set(report.missing_classes) == set(COVERAGE_CLASSES)


def test_the_report_says_which_classes_were_covered(builder, registry) -> None:
    class OnlyFoundational:
        name = "partial"
        kind = "bundle"
        reliability = 0.5

        def candidates(self, request):
            if request.coverage_class != "foundational":
                return ()
            return [
                Candidate(
                    statement=f"ふつうのこと {request.period_start.year}",
                    coverage_class="foundational",
                    available_from=request.period_start,
                )
            ]

    registry.register(OnlyFoundational())
    planner = CoveragePlanner(builder=builder, providers=registry, window_days=365)

    report = planner.run(period_start=PERIOD_START, period_end=PERIOD_END)

    assert report.classes_covered == ("foundational",)
    assert "environmental" in report.missing_classes
    assert report.as_detail()["candidates_stored"] == report.candidates_stored


def test_a_candidate_outside_its_job_period_is_rejected_not_clipped(
    builder, registry
) -> None:
    """A job for the nineties must not quietly absorb 2020 material."""

    class Leaky:
        name = "leaky"
        kind = "bundle"
        reliability = 0.5

        def candidates(self, request):
            return [
                Candidate(
                    statement="ずっとあとのできごと",
                    coverage_class=request.coverage_class,
                    available_from=datetime(2030, 1, 1, tzinfo=timezone.utc),
                )
            ]

    registry.register(Leaky())
    planner = CoveragePlanner(builder=builder, providers=registry, window_days=365)

    report = planner.run(period_start=PERIOD_START, period_end=PERIOD_END)

    assert report.candidates_stored == 0
    assert report.errors


def test_a_backwards_period_is_refused(builder, registry) -> None:
    planner = CoveragePlanner(builder=builder, providers=registry)
    with pytest.raises(ValueError):
        planner.plan(period_start=PERIOD_END, period_end=PERIOD_START)


# --- the authority is not a model -------------------------------------------
def test_no_provider_reaches_for_the_model() -> None:
    """Patch spec 16.2: ``Authorityはpretrained LLM knowledgeではなく外部source``."""
    source = (REPO_ROOT / "app" / "knowledge" / "providers.py").read_text(encoding="utf-8")
    for forbidden in ("StructuredGenerator", "LLMClient", "app.llm"):
        assert forbidden not in source, (
            f"{forbidden} in the knowledge providers would make what a model "
            "recalls the authority on what was public"
        )

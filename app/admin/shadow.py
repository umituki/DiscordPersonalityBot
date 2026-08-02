"""Building and replaying into a shadow database (patch spec 23.4).

:mod:`app.admin.repair` decides *whether* and *when*; this decides *how*, and
is kept separate so the repair service never assembles a world of its own.

Two operations, both against a shadow file and never against production:

``rebuild_genesis``
    Lives the same life again with the patched engine, from the original seed
    and scaffold, and puts it through the strengthened FIRST BOOT audits.

``replay_real_history``
    Re-processes the real Discord events that already happened, so their
    psychological effect is rebuilt on top of the repaired past.

The replay constructs no gateway and no conversation service. Steps 7-9 of
23.4 — no Discord side effects, what YUI said stays what she said, no reply is
regenerated — are properties of what is *not* built here, not of a flag
somebody remembered to pass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from app.bootstrap import Application
from app.clock import Clock
from app.config import AppConfig
from app.conversation.events import USER_MESSAGE_RECEIVED, YUI_MESSAGE_SENT
from app.events.model import Event
from app.simulation.genesis import GenesisReport
from app.simulation.models import LifeScaffold, TemperamentSeed

logger = logging.getLogger(__name__)

MODULE = "shadow_rebuild"

#: Event types whose conversation projection has to be rebuilt with them.
#: The projection is a read model over the archive (spec 2.9), so it is
#: reconstructed rather than carried across.
PROJECTED = (USER_MESSAGE_RECEIVED, YUI_MESSAGE_SENT)


def shadow_config(config: AppConfig, shadow_path: Path) -> AppConfig:
    """The same configuration, pointed at a different database file."""
    return config.model_copy(
        update={
            "paths": config.paths.model_copy(
                update={"data_dir": shadow_path.parent}
            ),
            "database": config.database.model_copy(
                update={"filename": shadow_path.name}
            ),
        }
    )


@dataclass(frozen=True, slots=True)
class ReplayReport:
    processed: int = 0
    projected: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.processed + self.skipped


async def rebuild_genesis(
    *,
    config: AppConfig,
    shadow_path: Path,
    seed: TemperamentSeed,
    scaffold: LifeScaffold,
    clock: Clock | None = None,
    max_blocks: int | None = None,
    on_built: "Callable[[Application], object] | None" = None,
) -> GenesisReport:
    """Live the original life again in a shadow database (23.4 steps 3-4).

    ``on_built`` is a hook for the caller to adjust the freshly built shadow
    application before it runs — a test points it at a local model double so a
    rebuild does not need a reachable one.
    """
    application = Application.build(
        shadow_config(config, shadow_path), clock=clock, configure_logs=False
    )
    if on_built is not None:
        on_built(application)
    try:
        rebuilt = application.simulation.prepare(
            seed,
            period_start=scaffold.period_start,
            period_end=scaffold.period_end,
            environment=scaffold.environment,
            education_context=scaffold.education_context,
            social_density=scaffold.social_density,
            technology_availability=scaffold.technology_availability,
            life_stage=scaffold.life_stage,
        )
        result = await application.simulation.run(rebuilt, max_blocks=max_blocks)
        report = await application.genesis.first_boot(result.run)
        logger.info(
            "shadow genesis booted=%s experiences=%d",
            report.booted,
            result.progress.experiences,
        )
        return report
    finally:
        application.db.close()


async def replay_real_history(
    *,
    config: AppConfig,
    shadow_path: Path,
    events: Sequence[Event],
    clock: Clock | None = None,
    on_built: "Callable[[Application], object] | None" = None,
) -> ReplayReport:
    """Re-process real events onto the repaired past (23.4 steps 5-10).

    The events are appended exactly as they were recorded — same ids, same
    payloads, same timestamps — and processed in ``replay`` mode so their
    effects land at the moment they originally happened rather than today.
    What YUI said is read out of the payload and projected; it is never
    regenerated, and nothing is sent.
    """
    application = Application.build(
        shadow_config(config, shadow_path), clock=clock, configure_logs=False
    )
    if on_built is not None:
        on_built(application)
    processed = 0
    projected = 0
    skipped = 0
    try:
        if not application.genesis.has_booted():
            raise RuntimeError(
                "the shadow database has not booted; replaying real history onto "
                "an unfinished past would put the USER before FIRST BOOT"
            )
        for event in events:
            outcome = await application.processor.process(event, mode="replay")
            if outcome.status == "failed":
                skipped += 1
                logger.error(
                    "replay failed event_id=%s error=%s", event.event_id, outcome.error
                )
                continue
            processed += 1
            if event.event_type in PROJECTED:
                projected += _project(application, event)
    finally:
        application.db.close()

    logger.info(
        "replay complete processed=%d projected=%d skipped=%d",
        processed,
        projected,
        skipped,
    )
    return ReplayReport(processed=processed, projected=projected, skipped=skipped)


def _project(application: Application, event: Event) -> int:
    """Rebuild one conversation turn from the archive (spec 2.9)."""
    payload = event.payload
    channel_id = getattr(payload, "channel_id", None)
    text = getattr(payload, "text", None)
    if not channel_id or not isinstance(text, str):
        return 0

    conversation = application.conversations.ensure_conversation(
        channel_id=str(channel_id),
        channel_type="direct_message" if getattr(payload, "is_direct_message", True)
        else "guild_text",
        now=event.occurred_at,
    )
    application.conversations.record_turn(
        conversation_id=conversation.conversation_id,
        event_id=event.event_id,
        speaker="user" if event.event_type == USER_MESSAGE_RECEIVED else "yui",
        author_id=event.actor_id if event.event_type == USER_MESSAGE_RECEIVED else None,
        content=text,
        occurred_at=event.occurred_at,
        message_ref=getattr(payload, "message_id", None),
    )
    return 1


__all__ = [
    "MODULE",
    "PROJECTED",
    "ReplayReport",
    "rebuild_genesis",
    "replay_real_history",
    "shadow_config",
]

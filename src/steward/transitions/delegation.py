"""The delegation transition: one accepted handoff, written and announced together.

One act, and it is deliberately the *last* thing that happens to a handoff. Every
guardrail — the sender is not retired, the recipient exists and is not itself, the
sender's manifest permits it, the receiver's route is declared, of the right kind, and
active, the parent is the task the sender actually holds, the chain is not too deep, and
it revisits nobody — is answered in :mod:`steward.delegation` **before** this is called,
and a refusal never reaches here at all. That is the shape the invariant needs: a refused
handoff writes nothing and emits nothing, and the way to guarantee that is for the write
and the fact to live behind one door that a refusal never opens.

The grammar stays in :mod:`steward.delegation` too — what a ``<delegate>`` block may say,
and what a refusal is called. This module records the letter and names both ends of it.
"""

from dataclasses import dataclass

from steward import events as ev
from steward.store import JobRecord, Store
from steward.transitions.outcome import Transition, applied

__all__ = ["DelegationTransitions"]


@dataclass(frozen=True, slots=True)
class DelegationTransitions:
    """The durable letter and its ``task_delegated`` fact, coordinated in one place."""

    store: Store
    emitter: ev.Emitter

    def deliver(  # noqa: PLR0913 — one keyword per column of the row or field of the fact
        self,
        *,
        title: str,
        detail: str,
        assignee: str,
        delegated_by: str,
        route: str,
        parent_task_id: str | None,
        origin: str | None,
        depth: int,
        sender_agent_id: str,
        sender_project: str,
        recipient_agent_id: str,
    ) -> Transition[JobRecord]:
        """Enqueue one accepted handoff and announce it. Always applied, by construction.

        There is no guard here and nothing to lose: an insert with a fresh id always wins,
        and every reason to refuse was answered before the call. A caller reaching this
        method has already decided the handoff is legitimate, so the only honest outcome is
        the one that writes the row and says so.

        Two identities are in play and they are not interchangeable. ``delegated_by`` and
        ``assignee`` are *resident ids*, because that is what an inbox is addressed by and
        what a lineage is walked with. ``sender_agent_id`` and ``recipient_agent_id`` are
        *burrow agent ids*, because that is what the village walks villagers by. The fact
        is emitted under the **delegating** resident, since the villager burrow has to walk
        across the village is the one carrying the letter.

        Nothing else in the chain needs a new event type: the receiver picks the item up
        with ``task_claimed`` and closes it with ``task_done``/``task_failed``, both
        carrying the same ``parent_task_id``, so the whole handoff is reconstructible from
        the log.
        """
        task = self.store.delegate_job(
            title=title,
            detail=detail,
            assignee=assignee,
            delegated_by=delegated_by,
            route=route,
            parent_task_id=parent_task_id,
            origin=origin,
            depth=depth,
        )
        return applied(
            self.emitter,
            task,
            ev.task_delegated_event(
                task_id=task.task_id,
                title=task.title,
                sender=sender_agent_id,
                recipient=recipient_agent_id,
                route=route,
                project=sender_project,
                depth=depth,
                parent_task_id=task.parent_task_id,
            ),
        )

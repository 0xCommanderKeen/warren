"""The org chart, computed from the declarations and nothing else (warren#441).

Who may hand work to whom is not a thing anybody decides in a drawing. It is
``delegation.send`` and ``delegation.to`` on the sending manifest, met by an *active*
route of kind ``delegation`` on the receiving one — the same two halves
:mod:`steward.delegation` checks before it delivers anything. A hand-drawn chart is a
second copy of that fact, and a second copy of a fact drifts: a grant added on a Tuesday
is on the chart only if somebody remembered the chart on Tuesday.

So the chart is a projection. Every node is one validated manifest, every edge is a pair
of declarations that agree, and nothing here reads the database, the host, or the clock:
:func:`org_chart` takes the residents :func:`steward.manifest.validate_path` already
produced and returns a value. That is what lets ``GET /org``, ``steward org`` and
Townhall's Org page show the same chart without any of them re-deriving it.

**An edge is drawn even when it will not deliver.** ``delegation.to: [pip]`` pointing at a
resident whose door is shut is a declared intention that does not work, and a chart that
silently dropped it would answer "there is no such grant" to a question about a grant that
is right there in the file. Such an edge carries ``deliverable: false`` and the reason, so
a panel can draw it dashed and a terminal can say why.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from steward.manifest import Mount, Resident

#: Why an edge that is declared will not carry work. Each one is the receiving half of the
#: pair failing, which is the only half an edge can fail on: the sending half is what made
#: the edge exist at all.
NO_SUCH_RESIDENT = "no resident is declared with that id"
RECEIVER_RETIRED = "the receiver is retired, so every door it had is closed"
NO_OPEN_ROUTE = "the receiver declares no active route of kind 'delegation'"


@dataclass(frozen=True)
class OrgMount:
    """One bind mount a resident's container is given, and on what terms."""

    host: str
    container: str
    mode: str

    @classmethod
    def of(cls, mount: Mount) -> OrgMount:
        """Project one declared mount."""
        return cls(host=mount.host, container=mount.container, mode=mount.mode)

    def to_dict(self) -> dict[str, Any]:
        """Return the wire form."""
        return {"host": self.host, "container": self.container, "mode": self.mode}


@dataclass(frozen=True)
class OrgBudget:
    """What one resident may spend in a day, as its manifest declares it.

    The *declared* caps, not today's spend: an org chart answers "what is this resident
    allowed to do", and a gauge that moved between two reads would make the same chart
    look different for no reason anybody could name. ``GET /residents/{id}/budget`` is
    where the ledger lives. ``declared: false`` with every cap ``null`` is said out loud,
    for the reason :class:`steward.manifest.Budgets` gives: unlimited must not read as
    unknown.
    """

    declared: bool
    daily_cost_usd: float | None
    daily_tokens: int | None
    max_run_seconds: int | None

    @classmethod
    def of(cls, resident: Resident) -> OrgBudget:
        """Project one resident's declared budgets."""
        budgets = resident.manifest.budgets
        return cls(
            declared=budgets.declared,
            daily_cost_usd=budgets.daily_cost_usd,
            daily_tokens=budgets.daily_tokens,
            max_run_seconds=budgets.max_run_seconds,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the wire form."""
        return {
            "declared": self.declared,
            "daily_cost_usd": self.daily_cost_usd,
            "daily_tokens": self.daily_tokens,
            "max_run_seconds": self.max_run_seconds,
        }


@dataclass(frozen=True)
class OrgNode:
    """One resident, as the chart draws it."""

    id: str
    uid: str
    name: str
    role: str
    #: The soul's hex accent, so a panel colours a node the way the village does.
    accent: str
    summary: str | None
    retired: bool
    #: How far below a resident that nobody delegates to this one sits. See :func:`_ranks`.
    rank: int
    #: The steward API write doors this resident's sessions may cross (warren#411).
    session_grants: tuple[str, ...]
    #: Declared third-party access, as ``id: status`` pairs.
    app_grants: tuple[tuple[str, str], ...]
    mounts: tuple[OrgMount, ...]
    budget: OrgBudget
    #: Whether the manifest permits this resident to hand work over at all.
    delegates: bool
    #: The ids of its own routes that delegated work may be delivered into today.
    accepts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the wire form."""
        return {
            "id": self.id,
            "uid": self.uid,
            "name": self.name,
            "role": self.role,
            "accent": self.accent,
            "summary": self.summary,
            "retired": self.retired,
            "rank": self.rank,
            "session_grants": list(self.session_grants),
            "app_grants": [{"id": grant, "status": status} for grant, status in self.app_grants],
            "mounts": [mount.to_dict() for mount in self.mounts],
            "budget": self.budget.to_dict(),
            "delegates": self.delegates,
            "accepts": list(self.accepts),
        }


@dataclass(frozen=True)
class OrgEdge:
    """One declared handoff: ``sender`` may give work to ``receiver``."""

    sender: str
    receiver: str
    #: True when ``delegation.to`` names the receiver; false when the allowlist is empty
    #: and the receiver is therefore reachable because it opened a door, not because the
    #: sender picked it. The two are different grants and a chart that flattened them
    #: would say a manager chose a worker it has never heard of.
    named: bool
    #: Whether both declarations agree: the sender may send here and the receiver has an
    #: open door. It is the edge's own answer and not a promise about a particular
    #: handoff — :data:`steward.delegation.DEFAULT_MAX_DEPTH` caps how far a *chain* may
    #: travel, and no edge can see the lineage it would be the next hop of.
    deliverable: bool
    #: Why not, when not. ``None`` on a deliverable edge.
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return the wire form."""
        return {
            "sender": self.sender,
            "receiver": self.receiver,
            "named": self.named,
            "deliverable": self.deliverable,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class OrgChart:
    """The whole fleet as nodes and the edges between them."""

    nodes: tuple[OrgNode, ...]
    edges: tuple[OrgEdge, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the wire form ``GET /org`` answers with."""
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }

    def node(self, resident_id: str) -> OrgNode | None:
        """Return the node with this id, or ``None`` when the fleet has no such resident."""
        return next((node for node in self.nodes if node.id == resident_id), None)


def _receiving_refusal(receiver: Resident | None) -> str | None:
    """Report why a declared edge would not deliver into this receiver, or ``None``."""
    if receiver is None:
        return NO_SUCH_RESIDENT
    if receiver.retired:
        return RECEIVER_RETIRED
    if not receiver.delegation_routes:
        return NO_OPEN_ROUTE
    return None


def _edges(residents: Sequence[Resident]) -> tuple[OrgEdge, ...]:
    """Every declared handoff in the fleet, senders in tree order.

    A retired sender contributes nothing: retirement closes every door it had, sending
    included, so an edge out of it would draw a handoff steward refuses. The receiving
    half is reported rather than filtered — see this module's docstring.
    """
    by_id = {resident.id: resident for resident in residents}
    edges: list[OrgEdge] = []
    for sender in residents:
        delegation = sender.manifest.delegation
        if sender.retired or not delegation.send:
            continue
        if delegation.to:
            targets: list[tuple[str, bool]] = [(target, True) for target in delegation.to]
        else:
            # An empty allowlist means "any receiver whose own manifest opened a door", so
            # the edges it produces are exactly the doors that are open — an unnamed edge
            # that would not deliver is not a declared intention, it is the absence of one.
            targets = [
                (other.id, False)
                for other in residents
                if other.id != sender.id and not other.retired and other.delegation_routes
            ]
        for target, named in targets:
            refusal = _receiving_refusal(by_id.get(target))
            edges.append(
                OrgEdge(
                    sender=sender.id,
                    receiver=target,
                    named=named,
                    deliverable=refusal is None,
                    reason=refusal,
                )
            )
    return tuple(edges)


def _ranks(residents: Sequence[Resident], edges: Sequence[OrgEdge]) -> Mapping[str, int]:
    """Place each resident strictly below everyone who may hand it work.

    A layout fact, computed here rather than in each surface, so the terminal's indentation
    and the panel's rows cannot disagree about who is above whom. Deliverable edges only:
    an edge that carries nothing does not make anybody a manager.

    The walk is a topological one taking the **longest** path, not the shortest, and the
    difference is the whole correctness of the picture. With ``a → b``, ``a → c`` and
    ``c → b``, a breadth-first pass reaches ``b`` from ``a`` in one hop and puts it on the
    same row as ``c`` — a card that says "takes work from c" sitting level with c. Waiting
    until every manager has been placed puts ``b`` under both.

    Two residents that may delegate to each other are a cycle, and a cycle has no top:
    nobody in one ever loses their last incoming edge, so the walk never reaches them.
    They keep rank 0 — drawn on the top row, where a reader can see the pair — rather than
    being dropped or given an invented order. Anything reachable only through a cycle
    keeps 0 for the same reason: there is no honest depth below a bottom that does not
    exist.
    """
    ranks = dict.fromkeys((resident.id for resident in residents), 0)
    below: dict[str, list[str]] = {resident.id: [] for resident in residents}
    managers = dict.fromkeys(ranks, 0)
    for edge in edges:
        if not edge.deliverable or edge.receiver not in ranks:
            continue
        below[edge.sender].append(edge.receiver)
        managers[edge.receiver] += 1

    frontier = [resident_id for resident_id, count in managers.items() if not count]
    while frontier:
        resident_id = frontier.pop()
        for receiver in below[resident_id]:
            ranks[receiver] = max(ranks[receiver], ranks[resident_id] + 1)
            managers[receiver] -= 1
            if not managers[receiver]:
                frontier.append(receiver)
    # Whatever the walk never placed is in a cycle or behind one. Its rank is the 0 it was
    # seeded with, said here rather than left as an accident of the loop above.
    return {
        resident_id: rank if not managers[resident_id] else 0 for resident_id, rank in ranks.items()
    }


def org_chart(residents: Iterable[Resident]) -> OrgChart:
    """Project the validated fleet as an org chart.

    Nodes come out in the order the tree produced them (which
    :func:`~steward.manifest.validate_path` sorts by id), so two calls over one unchanged
    tree are the same bytes.
    """
    ordered = list(residents)
    edges = _edges(ordered)
    ranks = _ranks(ordered, edges)
    nodes = tuple(
        OrgNode(
            id=resident.id,
            uid=resident.uid,
            name=resident.manifest.soul.name,
            role=resident.manifest.soul.role,
            accent=resident.manifest.soul.accent,
            summary=resident.manifest.summary,
            retired=resident.retired,
            rank=ranks[resident.id],
            session_grants=tuple(str(grant) for grant in resident.manifest.session_grants),
            app_grants=tuple((grant.id, grant.status) for grant in resident.manifest.app_grants),
            mounts=tuple(OrgMount.of(mount) for mount in resident.manifest.deploy.mounts),
            budget=OrgBudget.of(resident),
            delegates=resident.manifest.delegation.send,
            accepts=resident.delegation_routes,
        )
        for resident in ordered
    )
    return OrgChart(nodes=nodes, edges=edges)

# Chronicle Village

Chronicle projects truthful agent activity into a village without turning the fiction into hidden system state.

## Language

**Villager**:
The visible projection of one event-producing agent identity. A villager is either a Resident or a Visitor.

**Resident**:
A villager matched by a complete, valid resident manifest. A resident owns one reserved Home and remains identifiable when absent.
_Avoid_: Souled session, permanent session

**Visitor**:
A villager without a matching valid resident manifest. Visitors have ephemeral event identities and share the Lodge.
_Avoid_: Unsouled resident, homeless villager

**Home**:
One stable village house reserved for exactly one Resident, including while that Resident is absent.
_Avoid_: Slot, active plot

**Lodge**:
The shared village base for every Visitor; it is never a Resident's Home.
_Avoid_: Overflow house

**Resident Manifest**:
A versioned declaration of a Resident's identity, soul, skills, durable memory reference, inbound routes, app grants, and Home. It describes capability status but contains no credential material.
_Avoid_: Soul file

**Lineage**:
Optional event metadata associating a child villager with its parent without merging their identities or lifecycles.
_Avoid_: Delegation destination

**Handoff**:
One villager giving work to a named Resident rather than to the open board, carried as a `task_delegated` event and opening the same kind of board row a post does.
_Avoid_: Delegation (the act, when a row is meant), forwarding

**Assignee**:
The Resident a Handoff is addressed to, named on the row it opened. Only the Assignee may claim that row, so an open row with an Assignee is not work anybody can take. A posted job has none.
_Avoid_: Owner, recipient, delegation destination

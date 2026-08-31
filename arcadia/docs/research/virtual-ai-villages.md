# Virtual AI Villages: Research Map and Design Direction for Arcadia

_Last researched: 24 August 2026_

This is a curated, implementation-oriented map of the research and open-source ecosystem behind persistent virtual societies populated by language-model agents. Links favor papers, project pages, repositories, and official documentation. “Believable” below means coherent and legible to a player; it does **not** mean that an agent is conscious or a scientifically valid human substitute.

## Executive summary

Arcadia should be a **game simulation with AI residents**, not a collection of chatbots attached to sprites. The strongest evidence points to a hybrid design:

1. A deterministic, authoritative world owns time, space, inventories, skills, needs, permissions, economics, and action outcomes.
2. Language models make occasional high-level choices and generate socially expressive dialogue; they never directly mutate world state.
3. Each resident has stable authored identity, mutable needs and beliefs, an episodic event log, derived relationship state, and periodically consolidated reflections.
4. Routine behavior is scheduled or utility-driven. Model calls happen at decision boundaries, surprises, important conversations, and plan failures.
5. Every decision is a validated structured command selected from affordances exposed by the world.
6. Believability must be measured over days, not impressive single conversations: identity consistency, causal coherence, goal follow-through, relationship continuity, diversity, player comprehension, latency, and cost.

This direction combines the main contribution of **Generative Agents**—observation, retrieval, reflection, and planning—with decades of game-AI and agent-based-modeling practice. The original Smallville study populated a Sims-like environment with 25 agents and found through ablations that observation, planning, and reflection each contributed to judged believability; its Valentine's-party example also demonstrated information diffusion and coordination from one initial intention ([paper](https://arxiv.org/abs/2304.03442), [official code](https://github.com/joonspk-research/generative_agents)). That is a compelling prototype, not proof that LLM populations predict people or societies.

Recent evaluation work reinforces the warning. SOTOPIA found substantial model differences and a hard subset on which even its strongest tested model fell below humans in goal completion and struggled with social commonsense and strategic communication ([paper](https://arxiv.org/abs/2310.11667), [code](https://github.com/sotopia-lab/sotopia)). LIFELONG-SOTOPIA reports declining goal achievement and believability over extended interactions, with memory helping but not closing the human gap ([paper](https://arxiv.org/abs/2506.12666)). LLM judges can themselves overestimate agents optimized for social interaction ([SOTOPIA-π](https://arxiv.org/abs/2403.08715)). Arcadia therefore needs observable, game-grounded metrics and human playtests—not “the model rated itself as believable.”

## What field is this?

“Virtual AI village” sits at the intersection of several traditions:

| Tradition | What it contributes to Arcadia | Representative primary sources |
|---|---|---|
| Generative agents | Natural-language memory, reflection, planning, dialogue | [Generative Agents](https://arxiv.org/abs/2304.03442), [Generative Agent Simulations of 1,000 People](https://arxiv.org/abs/2411.10109) |
| Agent-based modeling (ABM) | Explicit rules, scheduling, emergence, repeatable experiments | [NetLogo](https://docs.netlogo.org/), [Mesa](https://mesa.readthedocs.io/), [GAMA](https://gama-platform.org/wiki/Introduction) |
| Believable agents / cognitive architectures | Goal-directed reactive behavior, emotion, personality, memory | [FAtiMA Toolkit](https://arxiv.org/abs/2103.03020), [PsychSim](https://ict.usc.edu/research/projects/psychsim-social-simulation/) |
| Social simulation in games | Relationships, social rules, authored actions, emergent narrative | [Comme il Faut](https://ojs.aaai.org/index.php/AIIDE/article/download/12454/12313/15982), [Anthology](https://ojs.aaai.org/index.php/AIIDE/article/view/21967) |
| Multi-agent LLM systems | Communication protocols, role specialization, orchestration | [CAMEL](https://arxiv.org/abs/2303.17760), [AutoGen](https://arxiv.org/abs/2308.08155), [MetaGPT](https://arxiv.org/abs/2308.00352) |
| Interactive narrative | Story direction without eliminating autonomy | [Mixing Story and Simulation](https://ojs.aaai.org/index.php/AIIDE/article/view/18770), [Continual Multiagent Planning](https://ojs.aaai.org/index.php/AAAI/article/view/7567) |
| Social-agent evaluation | Goal completion, social commonsense, safety, longitudinal coherence | [SOTOPIA](https://arxiv.org/abs/2310.11667), [LIFELONG-SOTOPIA](https://arxiv.org/abs/2506.12666) |

## Foundational architecture

### 1. Authoritative world simulation

The world model should be the source of truth. It advances a logical clock, schedules agents, resolves movement and collisions, enforces action preconditions, applies effects, and emits immutable events. A model may propose `give(item, target)`; only the world can decide whether the item exists, the target is reachable, and ownership changes.

This follows classic social-simulation systems: Comme il Faut represents authored social exchanges through explicit state and rules ([paper](https://ojs.aaai.org/index.php/AIIDE/article/download/12454/12313/15982)); Anthology combines motives, relationship knowledge, precondition/effect actions, geography, and a live inspector ([paper](https://ojs.aaai.org/index.php/AIIDE/article/view/21967)); PsychSim uses decision-theoretic agents with beliefs about other agents ([official project](https://ict.usc.edu/research/projects/psychsim-social-simulation/), [code](https://github.com/usc-psychsim/psychsim)). These systems remain valuable because explicit state is inspectable, testable, and cheap.

Suggested split:

```text
simulation tick (frequent, deterministic)
  movement / needs / jobs / production / weather / action execution

decision boundary (infrequent, model-assisted)
  perceive -> retrieve -> deliberate -> propose action -> validate -> execute

reflection cycle (rare, asynchronous)
  consolidate episodes -> beliefs / relationship interpretations / new goals
```

### 2. Agent state: separate fact, belief, and personality

Maintain distinct layers:

- **Soul / identity:** biography, voice, values, traits, fears, preferences, commitments. Mostly stable and authored.
- **Capabilities:** numerical skills and unlocked actions. Outcomes are computed by game rules.
- **Physical state:** position, needs, inventory, health, current task.
- **Goals and intentions:** long-term goals, current plan, commitments made to others.
- **Subjective beliefs:** propositions with provenance, confidence, and last update. These may be false.
- **Relationships:** asymmetric affinity, trust, familiarity, obligation, fear, attraction, and evidence/events behind each value.
- **Memory:** raw episodes plus consolidated summaries; never treat the full transcript as identity.

False and asymmetric beliefs are essential for gossip, discovery, betrayal, and dramatic irony. Theory-of-mind systems such as PsychSim explicitly model beliefs about other entities, while social-planning work shows how reasoning about and influencing others' minds can generate emergent narrative ([PsychSim](https://ict.usc.edu/research/projects/psychsim-social-simulation/), [social planning](https://ojs.aaai.org/index.php/AIIDE/article/view/18666)).

### 3. Memory, reflection, and retrieval

The Generative Agents architecture records observations in natural language, scores/retrieves memories, synthesizes higher-level reflections, and uses them in planning ([paper](https://arxiv.org/abs/2304.03442)). Earlier believable-agent work also demonstrated context-dependent and recency-sensitive autobiographical recall ([Kope, Rose & Katchabaw](https://ojs.aaai.org/index.php/AIIDE/article/view/12686)).

For Arcadia, use several stores rather than one undifferentiated vector database:

- **Canonical event log:** append-only, compact, fully auditable.
- **Working context:** current scene, perception, plan, and recent turns.
- **Episodic memories:** selected experienced events, tied to event IDs.
- **Semantic beliefs:** normalized claims with confidence and provenance.
- **Relationship ledger:** interactions and derived scores.
- **Reflections:** lossy summaries that point back to evidence.

Retrieval should combine semantic relevance with recency, importance, participant, place, active goal, and unresolved commitments. Embeddings alone can return thematically similar but causally irrelevant memories. Consolidation should preserve links to source events so a debugger—and eventually a player-facing journal—can explain why an agent believes something.

Longitudinal evaluation matters: LIFELONG-SOTOPIA found that tested agents' performance degraded across episodes even when memory methods helped ([paper](https://arxiv.org/abs/2506.12666)). Build memory regression tests early: promises survive save/load, rumors preserve provenance, summaries do not invert facts, and relevant old events can beat irrelevant recent events.

### 4. Planning and action selection

Use layered control:

- **Schedule** handles habits: sleep, meals, shop opening, ordinary work.
- **Utility scoring** handles routine choice under needs and motives.
- **Planner/LLM** handles novelty, social dilemmas, conflicting goals, and recovery.
- **Reactive interrupts** handle fires, attacks, blocked paths, and urgent needs.
- **Director** may introduce opportunities or constraints but should not force dialogue outcomes.

Goal-directed yet reactive execution has long been associated with believable game agents ([Choi et al.](https://ojs.aaai.org/index.php/AIIDE/article/view/18787)); continual multi-agent planning explicitly addresses changing beliefs, sentiments, goals, and plans that are thwarted or abandoned ([Brenner](https://ojs.aaai.org/index.php/AAAI/article/view/7567)). Hybrid story direction can issue desired world-state directives while preserving local autonomy and repairing the story when player action causes inconsistency ([Riedl, Stern & Dini](https://ojs.aaai.org/index.php/AIIDE/article/view/18770)).

The model sees only legal affordances and returns a schema-validated proposal:

```json
{
  "action": "talk_to",
  "target": "mara",
  "intent": "ask_about_missing_tools",
  "commitment": null,
  "confidence": 0.73
}
```

Avoid exposing coordinates, SQL, arbitrary code, or direct state-write tools. A failed proposal should produce a typed result (`target_unavailable`, `insufficient_item`, `closed`) that the controller can handle without inventing success.

### 5. Dialogue and social behavior

Dialogue is an action with consequences, not a parallel chat product. Before generation, decide intent, participants, privacy, known facts, allowed disclosures, relationship posture, and conversational stakes. After it, extract only validated game-relevant speech acts—promise, gift request, invitation, threat, rumor transmission—and link them to the transcript/event.

SOTOPIA evaluates agents across cooperation, competition, exchange, and complex social goals and found hard cases involving commonsense and strategic communication ([paper](https://arxiv.org/abs/2310.11667)). Arcadia should consequently test hidden goals, unequal information, refusal, negotiation, interruption, and repairing misunderstandings—not only friendly conversations.

### 6. Emergence and narrative

Emergence requires coupled systems: scarcity changes work; work changes skills and trade; trade changes obligations; obligations and gossip change relationships; relationships change future choices. Random dialogue without persistent effects is activity, not emergence.

Create “story pressure” through world conditions rather than scripts: an approaching winter, a contested job, a missing tool, a festival requiring coordination, or unevenly distributed knowledge. Allow a lightweight drama manager to select pressures and pace reveals. The literature offers both fully emergent social planning ([social planning](https://ojs.aaai.org/index.php/AIIDE/article/view/18666)) and hybrid high-level direction ([story/simulation integration](https://ojs.aaai.org/index.php/AIIDE/article/view/18770)); Arcadia should occupy the middle.

## Evidence and representative systems

### Generative societies and human proxies

- **Generative Agents: Interactive Simulacra of Human Behavior** — the direct Smallville foundation: 25 agents; memory stream, retrieval, reflection, planning; human believability evaluation and ablations. [Paper](https://arxiv.org/abs/2304.03442) · [official demo/code](https://github.com/joonspk-research/generative_agents)
- **Generative Agent Simulations of 1,000 People** — creates interview-grounded agents from two-hour qualitative interviews and evaluates their answers across social-science measures; important for identity construction and for its privacy implications. [Paper](https://arxiv.org/abs/2411.10109) · [official code](https://github.com/StanfordHCI/genagents)
- **AgentSociety** — reports simulations exceeding 10,000 agents and five million interactions across polarization, inflammatory messages, universal basic income, and hurricane scenarios. Treat its alignment claims as task-specific results, not general societal validity. [Paper](https://arxiv.org/abs/2502.08691) · [official code](https://github.com/tsinghua-fib-lab/agentsociety)
- **LMAgent** — reports a multimodal e-commerce society above 10,000 agents using fast memory and small-world interaction topology, with behaviors including browsing, buying, reviewing, and live streaming. [Paper](https://arxiv.org/abs/2412.09237)
- **Concordia** — a library for generative social simulation built around game-master-mediated environments and configurable agent components. [Paper](https://arxiv.org/abs/2312.03664) · [DeepMind repository](https://github.com/google-deepmind/concordia)
- **Humanoid Agents** — Minecraft-like sandbox agents with needs, social relationships, and natural-language plans; useful as an adjacent embodied-world prototype. [Paper](https://arxiv.org/abs/2310.05418) · [code](https://github.com/HumanoidAgents/HumanoidAgents)
- **Project Sid** — reports a Minecraft civilization simulation with more than 1,000 autonomous agents and studies roles, norms, governance, and cultural transmission. It is an ambitious demonstration, but its reported emergent episodes are not a benchmark of general social fidelity. [Paper](https://arxiv.org/abs/2411.00114)

### Evaluation and benchmarks

- **SOTOPIA / SOTOPIA-Eval** — open-ended two-agent social scenarios, multi-dimensional evaluation, human comparisons, and a difficult subset. [Paper](https://arxiv.org/abs/2310.11667) · [repository](https://github.com/sotopia-lab/sotopia)
- **SOTOPIA-π** — interactive social-agent learning; also finds that LLM evaluators can overestimate agents trained for the target interaction distribution. [Paper](https://arxiv.org/abs/2403.08715)
- **LIFELONG-SOTOPIA** — evaluates multi-episode social continuity and reports degradation over time despite advanced memory. [Paper](https://arxiv.org/abs/2506.12666)
- **AgentBench** — evaluates LLM agents in eight interactive environments; useful for general agent reliability, though not village believability. [Paper](https://arxiv.org/abs/2308.03688) · [code](https://github.com/THUDM/AgentBench)
- **AgentBoard** — fine-grained progress-rate evaluation and visualization for agents across environments. [Paper](https://arxiv.org/abs/2401.13178) · [code](https://github.com/hkust-nlp/AgentBoard)
- **MemoryAgentBench** — decomposes long-term agent memory into accurate retrieval, test-time learning, long-range understanding, and selective forgetting; these are better targets than a single retrieval score. [Paper](https://arxiv.org/abs/2507.05257)
- **Human-like NPC evaluation** — argues for behavior comparison against human experimental data rather than surface impression alone. [AAAI AIIDE paper](https://ojs.aaai.org/index.php/AIIDE/article/view/12439)

### Memory and agent-control patterns

- **ReAct** interleaves reasoning and tool actions; useful conceptual grounding for observe/act/result loops. [Paper](https://arxiv.org/abs/2210.03629) · [project](https://react-lm.github.io/)
- **Reflexion** stores linguistic feedback from past attempts to improve later trials; relevant to skill learning but should not overwrite canonical world facts. [Paper](https://arxiv.org/abs/2303.11366) · [code](https://github.com/noahshinn/reflexion)
- **MemGPT** treats context management as tiered virtual memory; useful for long-lived agents whose history exceeds a context window. [Paper](https://arxiv.org/abs/2310.08560) · [code](https://github.com/cpacker/MemGPT)
- **Voyager** combines an automatic curriculum, iterative prompting, and a reusable code skill library in Minecraft; it demonstrates open-ended skill acquisition, not a social village architecture. [Paper](https://arxiv.org/abs/2305.16291) · [code](https://github.com/MineDojo/Voyager)
- **FAtiMA Toolkit** provides open-source emotion appraisal and decision-making components for virtual agents and social robots. [Paper](https://arxiv.org/abs/2103.03020) · [code](https://github.com/GAIPS/FINAL)
- **Autobiographical memory for believable agents** models hierarchical memory with context-dependent recall and recency effects in a Minecraft proof of concept. [Paper](https://ojs.aaai.org/index.php/AIIDE/article/view/12686)

### Social and narrative simulation before LLMs

- **Comme il Faut (CiF)** — authorable social state, social exchanges, rules and cultural knowledge; powered _Prom Week_. [Paper](https://ojs.aaai.org/index.php/AIIDE/article/download/12454/12313/15982)
- **Social Story Worlds with Comme il Faut** — fuller account of CiF and its use in _Prom Week_. [Paper](https://www.cs.uky.edu/~sgware/reading/papers/mccoy2014cif.pdf)
- **Anthology** — reusable framework combining motives, geography, relationships, actions, and inspection. [Paper](https://ojs.aaai.org/index.php/AIIDE/article/view/21967) · [repository](https://github.com/ianhorswill/anthology)
- **PsychSim** — decision-theoretic social agents and theory of mind, used in training and social-simulation applications. [Official page](https://ict.usc.edu/research/projects/psychsim-social-simulation/) · [repository](https://github.com/usc-psychsim/psychsim)
- **Continual Multiagent Planning** — integrates planning and execution with epistemic and affective states in changing story worlds. [Paper](https://ojs.aaai.org/index.php/AAAI/article/view/7567)
- **Simulation-Based Story Generation with a Theory of Mind** — social planning through beliefs and influence. [Paper](https://ojs.aaai.org/index.php/AIIDE/article/view/18666)
- **Mixing Story and Simulation** — combines semi-autonomous characters with adaptive high-level story directives. [Paper](https://ojs.aaai.org/index.php/AIIDE/article/view/18770)
- **Never a Dull Moment** — combines ABL, CiF, and world events to preserve physical/mental character context. [Paper](https://ojs.aaai.org/index.php/AIIDE/article/view/21974)

## Tools and building blocks

These are candidates to inspect, not a recommendation to combine all of them.

### Agent and social-simulation frameworks

- [Google DeepMind Concordia](https://github.com/google-deepmind/concordia) — research framework for game-master-mediated generative social simulation.
- [Stanford Generative Agents](https://github.com/joonspk-research/generative_agents) — reference Smallville implementation; research prototype rather than production game backend.
- [Stanford genagents](https://github.com/StanfordHCI/genagents) — interview/demographic agent banks, memory, reflection, and survey interactions.
- [AgentSociety](https://github.com/tsinghua-fib-lab/agentsociety) — large-scale LLM social simulation platform.
- [SOTOPIA](https://github.com/sotopia-lab/sotopia) — social scenarios and evaluation.
- [AI Town](https://github.com/a16z-infra/ai-town) — deployable open-source virtual town inspired by Smallville; especially useful as an implementation comparison ([architecture](https://github.com/a16z-infra/ai-town/blob/main/ARCHITECTURE.md)). Its issue tracker also provides concrete operational evidence of timeouts, generation mismatch, and conversation loops ([issue #284](https://github.com/a16z-infra/ai-town/issues/284)).
- [TinyTroupe](https://github.com/microsoft/TinyTroupe) — Microsoft's experimental persona simulation library for multi-agent interactions and qualitative research-style scenarios.
- [PsychSim](https://github.com/usc-psychsim/psychsim) — explicit decision-theoretic social models.
- [FAtiMA](https://github.com/GAIPS/FINAL) — affective appraisal and social-agent decision making.
- [Mesa](https://github.com/mesa/mesa) — Python ABM with spaces, activation/scheduling, data collection, browser visualization and examples ([documentation](https://mesa.readthedocs.io/)).
- [NetLogo](https://www.netlogo.org/) — mature programmable modeling environment with an extensive model library ([manual](https://docs.netlogo.org/)).
- [GAMA](https://github.com/gama-platform/gama) — spatially explicit agent simulation with multiple behavioral architectures ([documentation](https://gama-platform.org/wiki/Introduction)).

### LLM orchestration

- [LangGraph](https://github.com/langchain-ai/langgraph) — stateful agent workflows; useful for durable decision/reflection jobs, but not a game simulation.
- [Microsoft AutoGen](https://github.com/microsoft/autogen) — event-driven multi-agent applications and agent messaging.
- [CAMEL](https://github.com/camel-ai/camel) — role-playing and multi-agent research framework.
- [CrewAI](https://github.com/crewAIInc/crewAI) — role/task-oriented agent workflow framework.
- [Semantic Kernel](https://github.com/microsoft/semantic-kernel) — model/tool orchestration SDK.

Workflow frameworks solve retries, tool calls, and message routing. They do not provide spatial simulation, a coherent economy, agent identity, or believability evaluation. Keep the domain model independent so orchestration technology can be replaced.

### Game and world layer

- [Phaser](https://github.com/phaserjs/phaser) — TypeScript/JavaScript 2D HTML5 game framework; a strong web-client candidate.
- [Godot](https://github.com/godotengine/godot) — open-source 2D/3D engine with navigation, animation, tilemaps, networking, and editor tooling.
- [Tiled](https://github.com/mapeditor/tiled) — open-source tilemap editor with JSON export.
- [Colyseus](https://github.com/colyseus/colyseus) — authoritative multiplayer state synchronization for Node.js.
- [PixiJS](https://github.com/pixijs/pixijs) — lower-level high-performance 2D renderer if Arcadia does not need Phaser's game abstractions.

Suggested MVP: Phaser + Tiled client, a TypeScript authoritative server and scheduler, Postgres/event log, WebSockets, and a small durable model-call queue. Add an agent framework only if the home-grown decision state machine becomes painful.

### State, memory, observability, and testing

- [PostgreSQL](https://www.postgresql.org/docs/) — canonical relational state, event log, JSON where schema is evolving.
- [pgvector](https://github.com/pgvector/pgvector) — optional semantic memory retrieval without creating a second database.
- [Redis](https://redis.io/docs/latest/) — ephemeral queues/cache/presence; do not make it the sole memory authority.
- [OpenTelemetry](https://opentelemetry.io/docs/) — traces spanning perception, retrieval, model call, validation, and execution.
- [Langfuse](https://github.com/langfuse/langfuse) — open-source LLM traces, prompt/version and evaluation tooling.
- [Promptfoo](https://github.com/promptfoo/promptfoo) — prompt and model regression/evaluation harness.

Store model/provider, prompt version, context IDs, output, validation result, latency, token use, and cost for every deliberation. Support deterministic seeds for non-model systems and replay from the event log. Because hosted models change, exact model-level reproducibility may remain impossible; record enough to diagnose behavioral drift.

## Evaluation plan for Arcadia

Evaluate the system at four levels:

| Level | Example measures |
|---|---|
| Action validity | Schema-valid rate, impossible-action rate, retry rate, validator rejection reasons |
| Individual continuity | Biography contradictions, remembered promises, plan completion, recovery from interruption, voice/personality stability |
| Social continuity | Belief provenance, rumor accuracy/mutation, reciprocal/asymmetric relationships, negotiation outcomes, norm violations and repair |
| Village dynamics | Resource balance, role diversity, information diffusion, clustering/polarization, repeated-run variance, player-rated legibility and interest |
| Operations | Calls per simulated day, cost per resident-day, p50/p95 latency, queue depth, memory growth, save/replay determinism |

Use a layered test suite:

1. **Deterministic unit/property tests:** actions never create resources accidentally; schedules respect time; relationship updates are bounded; dead/unavailable actors cannot act.
2. **Scenario tests:** missing tools, festival planning, food shortage, broken promise, secret leak, disputed fact, newcomer integration.
3. **Long-horizon simulations:** run many seeds for 7–30 in-game days; detect loops, social collapse, homogenization, memory bloat, and stalled economies.
4. **Human playtests:** blinded comparisons of architecture variants; ask players what each character wants, why an event happened, and whether behavior remained coherent.
5. **Ablations:** remove reflection, provenance, schedules, relationship state, or director pressure one at a time. The original Generative Agents study's component ablations are the relevant precedent ([paper](https://arxiv.org/abs/2304.03442)).

Do not collapse everything into one “believability” score. Report validity, goal success, consistency, diversity, and player experience separately. Keep some human-scored samples because SOTOPIA-π shows that an LLM evaluator can be systematically overgenerous to agents optimized under similar judgments ([paper](https://arxiv.org/abs/2403.08715)).

## Major gaps and risks

### Scientific validity

Language models imitate patterns in their training data; a plausible population is not a calibrated population. Results are sensitive to prompts, model version, temperature, persona construction, interaction network, scheduling, available actions, and the game master's interventions. Claims about real-world policy or human groups require external data, baselines, sensitivity analysis, and domain ethics review. Arcadia's safe claim is “fictional emergent character simulation,” not “digital society prediction.”

### Long-horizon drift and identity collapse

Summaries can compound errors; residents may converge toward the model's default voice, forget commitments, or rationalize incompatible actions. LIFELONG-SOTOPIA provides direct evidence that social-agent goal achievement and believability can deteriorate across interactions ([paper](https://arxiv.org/abs/2506.12666)). Protect stable identity fields, preserve event evidence, and measure drift.

### Evaluation circularity

Using one model family to generate behavior and judge it risks shared biases. SOTOPIA-π observed judge overestimation after social-interaction training ([paper](https://arxiv.org/abs/2403.08715)). Mix executable checks, independent models, and humans; publish individual metrics.

### Cost and latency

Twenty agents thinking every few seconds is unnecessary and expensive. Routine schedules, event-triggered deliberation, batched offline reflection, cached plans, small models for extraction, and simulation sleep/off-screen time compression are architectural requirements. Budget calls per **resident-day**, not per wall-clock minute.

### Prompt injection and unsafe tool use

Players, other agents, books, signs, or imported world text can carry adversarial instructions. Treat all world content as data; expose a fixed action schema; enforce authorization and preconditions in code; cap outputs and tool loops. An agent's speech cannot grant database or administrative authority. OWASP explicitly notes that retrieval and fine-tuning do not fully eliminate prompt injection ([LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)); its [Agentic AI Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html) recommends least privilege, memory provenance, validation, monitoring, and budget/rate limits.

### Privacy and anthropomorphism

Do not construct residents from real private communications without meaningful consent. The 1,000-person generative-agent project restricts individual-level access because interview-grounded agents create privacy and ethics concerns ([official repository](https://github.com/StanfordHCI/genagents)). UI copy should describe simulated characters, memories, and generated thoughts without implying consciousness.

### Emergence can be boring or pathological

Autonomy alone does not guarantee stories. Agents can loop, idle, become uniformly agreeable, form irreversible grudges, or exhaust the economy. Use systemically meaningful pressures, decaying/reparable relationship mechanics, novelty detection, stuck-state recovery, and a restrained director.

### Reproducibility and provider drift

Model aliases, safety behavior, and inference nondeterminism change. Pin model snapshots where available, version prompts/schemas, record full decision traces, create replayable fixtures, and keep world execution deterministic. Never make old saves depend on rerunning historic LLM decisions.

### Safety and governance baseline

The [NIST Generative AI Profile](https://doi.org/10.6028/NIST.AI.600-1) provides a broad risk-management baseline covering confabulation, privacy, harmful content, human-AI configuration, and anthropomorphism. Arcadia should keep retention controls and moderation boundaries explicit, label generated character behavior honestly, provide report/reset controls, and avoid framing residents as conscious dependents.

## Recommended Arcadia direction

### Product thesis

Build a small, deeply simulated village where the player can understand why residents act, relationships have persistent consequences, and surprising stories emerge from limited resources and partial knowledge. Optimize for **legible causality**, not population size or unrestricted dialogue.

### Technical principles

1. **Events first:** every durable change originates in a typed world event.
2. **Facts are not beliefs:** the server knows truth; residents know observations and reports.
3. **Models propose; rules dispose:** all effects pass through deterministic validation.
4. **Sparse intelligence:** spend model calls at high-value decision boundaries.
5. **Inspectable souls:** surface goals, recent memories, relationships, and reasons in a spectator/debug view.
6. **Evidence-linked reflection:** summaries never replace or detach from source events.
7. **Replaceable models:** domain interfaces do not depend on one provider or orchestration SDK.
8. **Evaluation is a feature:** every simulation produces traces and metrics from day one.

Use three clocks: render time, simulation/event time, and asynchronous cognition jobs. Execute each high-level decision transactionally: snapshot perception → retrieve memories → propose typed intent → validate against current version → reserve contested resources → execute deterministic action → append outcome → derive memories. This prevents two agents buying one item, distant agents conversing, or stale plans overwriting current state.

### A focused first experiment

Five residents, one 20×20-ish map, four buildings, three resources, six actions (`move`, `work`, `eat`, `rest`, `talk`, `give`), a day/night schedule, bilateral relationships, private beliefs, promises, and episodic memory. Scenario: the village must gather enough food before winter; one resident knows a better farming technique, one hoards, and two begin with a grievance.

Instrument whether residents:

- transmit knowledge through actual conversations;
- remember who helped or broke a promise;
- revise false beliefs when shown evidence;
- coordinate without impossible actions;
- maintain distinct behavior over seven in-game days;
- produce a player-understandable chain from event to memory to decision to consequence.

Only after this passes should Arcadia add romance, family, construction, free-form crafting, larger populations, or agent-authored tools.

### Suggested milestones

1. **Simulation kernel:** clock, map, actions, event log, deterministic replay.
2. **Resident model:** identity, needs, skills, schedules, beliefs, relationships.
3. **Decision adapter:** affordance generation, structured output, validator, retry/fallback.
4. **Memory:** episodic selection, provenance-aware retrieval, reflection, save/load tests.
5. **Social loop:** dialogue intents, promises, rumor transfer, relationship consequences.
6. **Spectator UX:** inspect residents, timeline, pause/speed, “why?” trace.
7. **Evaluation harness:** scenarios, long runs, ablations, cost and drift dashboards.
8. **Playable vertical slice:** food-before-winter scenario and human playtest.

## Reading path

For the shortest useful path, read these in order:

1. [Generative Agents](https://arxiv.org/abs/2304.03442) — the immediate architecture and evaluation precedent.
2. [Comme il Faut](https://ojs.aaai.org/index.php/AIIDE/article/download/12454/12313/15982) — explicit, authorable social rules and exchanges.
3. [Concordia](https://arxiv.org/abs/2312.03664) — a game-master formulation for generative social simulation.
4. [SOTOPIA](https://arxiv.org/abs/2310.11667) — how to test social competence rather than admire transcripts.
5. [LIFELONG-SOTOPIA](https://arxiv.org/abs/2506.12666) — why persistence is the hard part.
6. [Anthology](https://ojs.aaai.org/index.php/AIIDE/article/view/21967) — motives, geography, relationships, actions, inspection.
7. [Generative Agent Simulations of 1,000 People](https://arxiv.org/abs/2411.10109) — persona grounding, evaluation, privacy, and limits.
8. [FAtiMA Toolkit](https://arxiv.org/abs/2103.03020) — emotion/appraisal beyond an adjective list in a prompt.

## Additional annotated bibliography

- **ReAct** — simple, influential reasoning/action loop for tool-using agents. [Paper](https://arxiv.org/abs/2210.03629)
- **Reflexion** — verbal reinforcement/reflection from feedback. [Paper](https://arxiv.org/abs/2303.11366)
- **CAMEL** — role-playing communication between agents. [Paper](https://arxiv.org/abs/2303.17760)
- **AutoGen** — conversable multi-agent application framework. [Paper](https://arxiv.org/abs/2308.08155)
- **MetaGPT** — role-specialized workflow with structured artifacts/communications. [Paper](https://arxiv.org/abs/2308.00352)
- **AgentVerse** — multi-agent collaboration and emergent behavior experiments. [Paper](https://arxiv.org/abs/2308.10848) · [code](https://github.com/OpenBMB/AgentVerse)
- **ChatDev** — communicative agents organized around a software-development process; useful as orchestration precedent, not village simulation. [Paper](https://arxiv.org/abs/2307.07924) · [code](https://github.com/OpenBMB/ChatDev)
- **Voyager** — long-horizon embodied skill acquisition in Minecraft. [Paper](https://arxiv.org/abs/2305.16291)
- **MemGPT** — tiered memory/context management. [Paper](https://arxiv.org/abs/2310.08560)
- **AgentBench** — multi-environment agent evaluation. [Paper](https://arxiv.org/abs/2308.03688)
- **AgentBoard** — interpretable fine-grained progress evaluation. [Paper](https://arxiv.org/abs/2401.13178)
- **MemoryAgentBench** — separates retrieval, learning, long-range understanding and forgetting. [Paper](https://arxiv.org/abs/2507.05257)
- **Human-like NPC behavior evaluation** — human-data-based fidelity metrics in strategic games. [Paper](https://ojs.aaai.org/index.php/AIIDE/article/view/12439)
- **Believable FPS agent** — cognitive architecture, reactive execution, and learning for plausible behavior. [Paper](https://ojs.aaai.org/index.php/AIIDE/article/view/18787)
- **Autobiographical memory for believable agents** — context and recency effects. [Paper](https://ojs.aaai.org/index.php/AIIDE/article/view/12686)
- **Toward personality and emotion** — overview of beliefs, goals, desires, affect, and personality as believable-character ingredients. [Paper](https://ojs.aaai.org/index.php/AIIDE/article/view/5253)
- **Integrating LLMs in agent-based social simulation** — argues for hybrid classical/SLM/LLM architectures and highlights fidelity, calibration and reproducibility issues. [Paper](https://arxiv.org/abs/2507.19364)
- **AI Agents Alone Are Not (Yet) Sufficient for Social Simulation** — calls for explicit environment, exposure, and scheduling mechanisms rather than attributing outcomes only to agents. [Paper](https://arxiv.org/abs/2603.00113)
- **Ambient AI for open worlds** — production-oriented hierarchy/subsumption and behavior-tree patterns for running hundreds of everyday NPC routines cheaply. [Paper](https://ojs.aaai.org/index.php/AIIDE/article/view/12705)
- **Behavior Multi-Queues** — explicit queued, interruptible and resumable collaborative behaviors and dynamic roles. [Paper](https://ojs.aaai.org/index.php/AIIDE/article/view/12350)
- **Statechart-Based AI** — layered statecharts, sensing, subsumption and parallel regions for responsive NPC control. [Paper](https://ojs.aaai.org/index.php/AIIDE/article/view/12515)
- **Façade architecture** — joint dialogue behaviors organized as story beats by a drama manager; a foundational example of authored direction plus character execution. [Paper](https://ojs.aaai.org/index.php/AIIDE/article/view/18722)

## Bottom line

The durable opportunity is not “put an LLM in every NPC.” It is to join authored game systems, explicit social state, subjective memory, and sparse model-assisted decisions into one replayable causal world. Arcadia wins if a player can watch a resident make a surprising choice, inspect the memories and relationships behind it, and then see that choice materially change village life.

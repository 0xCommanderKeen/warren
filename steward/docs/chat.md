# Chat: talking to a resident from a phone (v0)

`routes: {kind: chat}` used to be a description. Since warren#108 it is a doorway: a
daemon long-polls one Telegram bot per resident, and every message from a named operator
fires **one ordinary session** whose final message is sent back as the reply.

Text in, text out. No buttons, no outbound escalations, no group chats, one transport.
What is deliberately *not* here is at the bottom.

## The shape, in five sentences

1. A resident becomes reachable by declaring `routes: [{kind: chat, address:
   telegram:<bot>, status: active}]`. Nothing else in the manifest changes.
2. The bot's token lives in **steward's environment**, under `STEWARD_CHAT_TOKEN_<BOT>` —
   never in the manifest, which is git.
3. `steward chat run` polls every reachable bot. A message from a user id in
   `STEWARD_CHAT_OPERATORS`, in a private chat, fires a session with the message as its
   task and the last few turns of the conversation as context.
4. The session's final message is redacted, bounded, and sent back into the conversation.
5. A message from anybody else is dropped **without a reply** and recorded as a
   `chat_message_dropped` event.

## Why each piece is the way it is

**One bot per resident.** Talking to Pip and talking to the librarian are two
conversations, with two names and two faces, in two threads on your phone. A single bot
multiplexing the fleet would need a routing convention in every message and would make
"who am I talking to" a thing you have to remember rather than a thing you can see.

**The address is a reference, and the token is environment.** `telegram:pip` names a bot;
`STEWARD_CHAT_TOKEN_PIP` holds the secret that speaks as it. That split is the same one
every credential in this system lives on, and it buys the property that matters: the
manifest can be read, reviewed, committed, rendered in townhall and pasted into an issue
without any of that being a disclosure. The variable name is derived from the address's
reference — upper-cased, with anything that is not a letter or a digit folded to `_` — so
`telegram:polica-librarian` reads `STEWARD_CHAT_TOKEN_POLICA_LIBRARIAN`, and the mapping is
readable from the manifest alone. `steward chat list` prints the variable's name and
whether it is set; it never prints its value, anywhere, in any format.

A pasted bot token is refused by manifest validation
(`SECRET_VALUE_PATTERNS`) and scrubbed out of anything steward sends, including a reply.

**Long polling, no webhook.** Every connection is outbound. Nothing on the internet gets a
way into the burrow, no reverse proxy learns a new route, and no certificate has to be
right for a message to arrive.

**A separate daemon, sharing the state directory.** `steward chat run` is its own process
beside `steward scheduler run` and `steward watchdog run`, sharing their `steward.db`. That
is exactly why warren#111 exists: the one-session-per-resident claim is a row in that
database, so a message arriving while a routine is firing finds the resident busy across
processes and is *told so* rather than opening a second session.

**Refused, never queued.** A busy resident answers "…is busy right now — …; send that
again in a minute." This is the API's 409 in sentence form and it is the same judgement: a
person asking for something *now* cannot be handed it later and told it was now. A queue of
chat sessions would spend real money answering questions the operator gave up on an hour
ago. The same holds for a **paused** resident — a budget cap refuses a message exactly as
it refuses a scheduled fire, with the one addition that the person standing at the door is
told why.

**A window, not a history.** Each conversation is a rolling JSONL file in the resident's own
memory directory — `<memory>/chat/<conversation>.jsonl`, on the host side of the mount for a
container-placed resident, so the session sees the same file at `<memory.path>/chat/`. The
last ten turns go into the prompt; twenty survive on disk. It is a file, so it survives a
restart, `cat` reads it, and nothing new had to be invented to store it.

**The transcript is context, and it sits under the charter.** It is injected as the last
*context* section — after the journal, the skills and the decisions, immediately before the
charter — because it is the freshest and the least trusted of them. Both the window and the
incoming message go through the same neutralize-and-cap treatment every injected string
gets, so no amount of typing into a chat can forge a section that outranks a hard rule. The
operator typed it, which is precisely why it is not exempt: an operator's account can be
taken, and an operator pastes things they were sent.

**Every message is a run.** Same admission, same budget, same runner seam, same run
registry row, same `routine_started` / `routine_finished` bracket in the village — under
the trigger `chat` and the ledger kind `chat`. `steward budget show --by-origin` attributes
it to `human:chat`. A chat session that dies is buried by the watchdog like a routine fire,
because unlike a board task there is no lease sweep behind it.

**A dispatch sweep follows every answered message.** The same call the scheduler makes after
its fires, so a resident that hands work to a neighbour mid-conversation has handed it over
by the time you have read the reply.

**Silence is the answer to a stranger.** A reply of any kind — a refusal included — tells
whoever found the bot that it is live and that something is behind it. So an unknown sender
gets nothing, and the attempt becomes a `chat_message_dropped` event carrying the route, the
address, the sender id and the reason. **Not the text**: a stranger's message is the one
string in this system written by somebody steward has no relationship with, and the village
renders what it is given. A group chat is dropped the same way even when an operator speaks,
because the reply would be readable by everyone else in the group.

> **Known gap.** `chat_message_dropped` is a type steward emits and chronicle does not yet
> accept: its `EVENT_TYPES` gate refuses anything outside its own set, so today the drop
> lands in steward's local event log (`STEWARD_EVENTS_FALLBACK`) and not in the village.
> It is in company — `task_delegated`, `task_session_finished` and `resident_restarted`
> are missing there too — and the fix is one line in `chronicle/protocol.py`, deliberately
> not made here. Until then, `grep chat_message_dropped ~/.chronicle/events.jsonl` is how
> you see who knocked.

**Messages have a shelf life.** Telegram holds undelivered updates for a day, so a bridge
that was down all night would otherwise come up and fire a session for every message that
arrived while nobody was listening — real money, spent answering questions the operator
gave up on and has since answered themselves. Anything older than the catch-up window
(300s, the scheduler's number and the scheduler's reason) is dropped with a log line, and
**not** replied to: the same restart hands over many of them at once, and a bot that says
"I was not running" twenty times in a row is the unprompted outbound storm this bridge
exists not to be. Sending it again is the operator's move, and it is one message.
`--catchup-seconds` moves the window.

## Operator setup

Everything below is done once, on the burrow. Nothing in it is committed.

### 1. Make the bot

In Telegram, talk to [@BotFather](https://t.me/BotFather):

```
/newbot
  name:     Pip            # the display name in your chat list
  username: <something>_bot # must be globally unique and end in "bot"
```

BotFather answers with a token shaped `123456789:AA…`. That token **is** the bot: whoever
holds it reads every message you send it and can speak as your resident. Treat it exactly
like a password.

Then, still in BotFather, so the bot behaves like a private assistant rather than a group
member:

```
/setjoingroups   -> Disable      # it cannot be added to groups at all
/setprivacy      -> Enable       # belt and braces; steward drops group messages anyway
/setdescription  -> whatever you like — this is the "what can this bot do" blurb
```

Repeat per resident. One bot each.

### 2. Find your own user id

Message [@userinfobot](https://t.me/userinfobot) (or any equivalent) and note the numeric
id it answers with. That number, and no other, is what
`STEWARD_CHAT_OPERATORS` holds.

### 3. Put the secrets on the burrow

In the steward deploy's `.env` on the NAS (`~/docker/steward/.env`) — the same file that
already holds `CHRONICLE_TOKEN` and `STEWARD_TOKEN`:

```sh
STEWARD_CHAT_OPERATORS=123456789          # your Telegram user id; comma-separated for more
STEWARD_CHAT_TOKEN_PIP=123456789:AA…      # the token BotFather issued for telegram:pip
```

`chmod 600` it. It is not in git and must not be.

### 4. Flip the route to active

In the manifest, in a commit:

```yaml
routes:
  - id: chat
    kind: chat
    address: telegram:pip
    status: active          # was: pending
```

`pending` is what a chat route ships as, and it is honest: a manifest cannot carry the
token that would make it real, so a route that claimed to be active in git would be a
declaration nobody could satisfy from the repo alone. Flip it in the same change as the
deploy that sets the variable.

### 5. Check before you start the daemon

```console
$ steward chat list
pip/chat: telegram:pip — reachable
  token:   STEWARD_CHAT_TOKEN_PIP (set)
```

`not reachable yet` names exactly what is missing — a `pending` status, an unset variable,
an address that is not `<transport>:<reference>`, a transport this build cannot carry.

### 6. Run the daemon

A third service beside the scheduler and the watchdog, in the steward compose file. It
needs the **same** `/data` volume — one `steward.db`, one state directory, and the resident
memory directories the transcripts live in — and the same `.env`:

```yaml
services:
  steward-chat:
    image: steward:latest
    container_name: steward-chat
    restart: unless-stopped
    env_file: .env
    environment:
      STEWARD_STATE: /data/state/scheduler.json
    volumes:
      - ./data:/data
      - ./residents:/residents:ro
    command: ["steward", "chat", "run", "--residents", "/residents"]
```

It reaches no docker socket and needs none: it starts no container and supervises nothing —
a container-placed session it fires is launched through the same runner seam the scheduler
uses, so **if any resident with a chat route is container-placed, this service needs
`/var/run/docker.sock` and a `docker` binary exactly as the scheduler does**
([docs/topology.md](topology.md)). Pip is locally placed, so the canary does not.

The daemon takes a lock (`<state dir>/chat.lock`) for its whole life and a second one
refuses to start: two pollers on one bot do not merely double the work, they steal each
other's messages — Telegram hands an update to whichever `getUpdates` asked first and
refuses the other.

### 7. Say hello

Open the bot in Telegram and send `are you alive?`. Within a poll you should get an answer
in Pip's own voice, and in the village a `routine_started` / `routine_finished` pair under
the trigger `chat`.

If nothing comes back:

| symptom | what it means |
|---|---|
| no reply at all | one of three silences, and the daemon's log says which: your user id is not in `STEWARD_CHAT_OPERATORS`, you are messaging in a group, or the message is older than the catch-up window. The first two are also a `chat_message_dropped` event. |
| "…is busy right now" | the scheduler or a dispatch has that resident. Send it again. |
| "…cannot answer right now: …" | a budget pause, or a memory directory the daemon cannot see. `steward budget show` and `steward doctor` say which. |
| `telegram getUpdates failed` in the log | the token is wrong, or the burrow cannot reach `api.telegram.org`. |

## Environment

| variable | meaning |
|---|---|
| `STEWARD_CHAT_OPERATORS` | Comma-separated Telegram user ids steward answers. Empty means nobody, and the daemon refuses to start rather than run as an open door. |
| `STEWARD_CHAT_TOKEN_<REF>` | The bot token for the address `<transport>:<ref>`, upper-cased with non-alphanumerics folded to `_`. One per resident. |
| `STEWARD_CHAT_API_URL` | Where the bot API lives. Defaults to `https://api.telegram.org`; the test suite points it at loopback so nothing in this repo can reach the real thing. |
| `STEWARD_CHAT_POLL_TIMEOUT_S` | How long one `getUpdates` waits for a message (default 25s). The socket timeout is this plus ten seconds. |

## Explicitly not in v0

- **`needs_human` and task completions pushed into the chat.** Those are *notifications* —
  one-way, nothing listens for a reply — and they already have a channel
  ([warren#114](../src/steward/notify.py), ntfy). This bridge only ever speaks when spoken
  to.
- **Approval buttons.** An approval is an authorisation, and "who pressed it" is a security
  question a v0 chat channel has no honest answer to.
- **Discord, or any second transport.** The seam is a `ChatTransport` protocol with two
  methods, so a second one is a class rather than a rewrite. There is no second one.
- **Editing, reactions, photos, voice notes.** `allowed_updates: ["message"]`, text only;
  anything else is ignored rather than half-answered.
- **A conversation the resident starts.** There is no outbound half here and there is not
  going to be one.

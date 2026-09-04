# Chat: talking to a resident from a phone (v0)

`routes: {kind: chat}` used to be a description. Since warren#108 it is a doorway: a
daemon long-polls one Telegram bot per resident, and every message from a named operator
fires **one ordinary session** whose final message is sent back as the reply.

Text in, text out. No buttons, no outbound escalations, no group chats, one shipped transport.
The one outbound thing is a routine that says `deliver: chat` (warren#385), below. What is
deliberately *not* here is at the bottom.

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

Chronicle accepts the type as of warren#276 and treats it as *ambient*: a stranger's knock
never puts its resident on the village map or makes it look awake, so the drop shows up as
a `chat_message_dropped` record in the snapshot's `diagnostics` (the door, who knocked, the
reason) and in that villager's history when it has one for its own reasons. Townhall's
**Diagnostics** page draws those records (warren#279) — one line per sender, per door, with
a count, so a knock storm reads as one fact — which is where an operator sees a knock without
`curl`. An older chronicle 400s the event and it stays in steward's local log
(`STEWARD_EVENTS_FALLBACK`). The deployed chat daemon pins that log and its undelivered
queue to `/data/events/chat.jsonl`, on the persistent data volume, so
`docker exec steward-chat grep chat_message_dropped /data/events/chat.jsonl` still finds
it after a deploy recreates the container. Each control-plane service has its own file
under `/data/events/`, keeping the fallback's per-file lock local to one process.

**A stranger may knock as often as they like; they may not fill the log.** Every message
that reaches a closed door is dropped, but only the first in a **catch-up window** per
`(door, sender, reason)` becomes an event — the rest are counted, and the count leaves as
`payload.suppressed` on the record that closes the window (`KnockLimiter`, warren#278).
`suppressed` is how many *other* knocks that one record stands for, so the number of knocks
is `1 + suppressed` and a lone knock reads `0`. This is why the fix is a limiter rather than
a filter: a flood is more interesting than a single knock, and a bound that turned two
hundred messages into one ordinary-looking record would hide the thing worth noticing. The
storm is reported when its window closes even if the sender has stopped — the daemon sweeps
at the end of every pass, idle ones included — and a window forced out by
`KNOCK_DOORS_TRACKED` hands its count to that same sweep rather than losing it.

The reason is in the key because there are exactly two of them and townhall folds knocks by
reason: counting them together would put a group chat's tally on the line that says "not an
operator".

Without this, the drop's own channels are what an outsider gets to spend: chronicle keeps
the newest 200 diagnostics and a bounded history per villager, so a few hundred knocks could
push a resident's real history out of the village and the projection's own complaints off the
Diagnostics page. Chronicle bounds the other end of the same problem — knocks get a share of
those channels rather than all of them — and the two halves are complementary: this one keeps
the volume down, that one holds even when this one is outrun.

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

In the steward deploy's `.env` on the NAS (`~/docker/warren/steward/.env`) — the same file that
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
| no reply at all | one of three silences, and the daemon's log says which: your user id is not in `STEWARD_CHAT_OPERATORS`, you are messaging in a group, or the message is older than the catch-up window. The first two are also a `chat_message_dropped` event — one per sender per door per reason per catch-up window, the repeats counted into `suppressed`. |
| "…is busy right now" | the scheduler or a dispatch has that resident. Send it again. |
| "…cannot answer right now: …" | a budget pause, or a memory directory the daemon cannot see. `steward budget show` and `steward doctor` say which. |
| `telegram getUpdates failed` in the log | the token is wrong, or the burrow cannot reach `api.telegram.org`. |

## Multiple routes and operator identities

A resident may declare more than one chat route as long as each address is distinct. The
address is the stable selection key: `telegram:hob` and `discord:hob` are different doors,
even though their references happen to match. Their tokens are distinct too: Telegram
keeps `STEWARD_CHAT_TOKEN_HOB`, while Discord uses
`STEWARD_CHAT_TOKEN_DISCORD_HOB`. Only transports compiled into the running control plane
are reachable; declaring a Discord route does not itself add Discord or stop a supported
route from working. Validation also refuses distinct-looking addresses that fold to the
same environment name (`discord:pip-prod` and `discord:pip.prod`, for example), so the
human-readable naming rule cannot alias two credentials.

`STEWARD_CHAT_OPERATORS` is one comma-separated list with transport-qualified identities:

```sh
STEWARD_CHAT_OPERATORS=telegram:123456789,discord:987654321
```

Each transport sees only its own ids. Existing bare values remain Telegram ids, so
`STEWARD_CHAT_OPERATORS=123456789` keeps its v0 meaning. Qualification matters once the
same numeric id could name unrelated accounts on two services; authorisation never crosses
that boundary.

## Delivered routines

`routines[].deliver: chat` is the one message a resident sends without being spoken to,
and it is a *routine's* message rather than the bridge's: the scheduler fires the routine
as ever, and after `routine_finished` hands the session's final message to
`RoutineDelivery`, which sends it into each operator's private conversation with the
resident's bot. The address is the route's own — `chat` names the route kind, not the
transport, so a route on a second transport delivers through that transport's
`ChatTransport`. Bare `chat` is valid only when exactly one active chat route exists. A
resident with several must name the address — for example `deliver: discord:hob` — and a
missing, pending, disabled, or undeclared address is a validation error. The send is the same
egress as a reply: redacted, *then* bounded, so a token the session printed never reaches
the phone.

`quiet_word` names the one reply that means "say nothing" (Hob's digest uses `NOTHING`);
an empty message is quiet too. Everything else goes as written.

The run row says what happened — `delivered`, `quiet`, or `delivery_failed` with the
reason — and the outcome is untouched by it: a phone that is off is not a failed routine.
A failed or timed-out run delivers nothing. The scheduler and the API both hold the same
`STEWARD_CHAT_TOKEN_<REF>` and `STEWARD_CHAT_OPERATORS` the bridge reads, so a run-now over
the API delivers exactly as a scheduled fire does; `steward chat run` need not be up for a
delivery to land. Field rules are in
[manifest.md](manifest.md#deliver-chat-an-addressed-delivery-and-quiet_word).

## Environment

| variable | meaning |
|---|---|
| `STEWARD_CHAT_OPERATORS` | Comma-separated `<transport>:<user-id>` identities steward answers. Bare ids remain Telegram-compatible. Empty means nobody, and the daemon refuses to start rather than run as an open door. |
| `STEWARD_CHAT_TOKEN_<REF>` / `STEWARD_CHAT_TOKEN_<TRANSPORT>_<REF>` | Telegram keeps the v0 token name; other transports include their name, so equal references cannot share credentials. Upper-cased with non-alphanumerics folded to `_`. One per route. |
| `STEWARD_CHAT_API_URL` | Where the bot API lives. Defaults to `https://api.telegram.org`; the test suite points it at loopback so nothing in this repo can reach the real thing. |
| `STEWARD_CHAT_DISCORD_API_URL` | Discord REST base URL. Defaults to `https://discord.com/api/v10`; tests override it with loopback. |
| `STEWARD_CHAT_DISCORD_GUILD` | Guild id whose allowlisted channel names are resolved for resident posts. |
| `STEWARD_CHAT_POLL_TIMEOUT_S` | How long one `getUpdates` waits for a message (default 25s). The socket timeout is this plus ten seconds. |

## Discord DMs

A `discord:<ref>` route is a second implementation of the same `ChatTransport` boundary.
It polls only the configured operators' DM channels and sends replies with
`POST /channels/{channel-id}/messages`; it does not connect to the Gateway and the bot
therefore appears offline. Guild messages and mentions are deliberately left to the future
Gateway worker below.

Create one application per resident in the Discord Developer Portal, add its bot, and copy
the bot token into `STEWARD_CHAT_TOKEN_DISCORD_<REF>`. Do not paste the token into a
manifest. Add `discord:<your-user-id>` to `STEWARD_CHAT_OPERATORS`, then declare an active
route such as `address: discord:pip`. Invite the bot with Discord's OAuth2 URL Generator:
select the `bot` scope; this DM-only transport requests no guild permissions. The operator
and bot must share a server before Discord allows the operator to open the DM.
The resulting zero-permission invite has this form (replace the placeholder with the
application's ID):

```text
https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot&permissions=0
```

No privileged intents are required. Discord documents that the Message Content restriction
applies to REST too, but explicitly exempts DMs sent to the application. Because this v0
polls REST rather than Gateway events, it also sends no `IDENTIFY` intents. A later Gateway
DM consumer needs the ordinary `DIRECT_MESSAGES` intent, not privileged Message Content,
for the same messages. See Discord's
[Message Content guidance](https://docs.discord.com/developers/gateway/you-might-not-need-a-privileged-intent#message-content-intent)
and [Get Channel Messages](https://docs.discord.com/developers/resources/message#get-channel-messages).

At startup steward opens one DM channel per configured Discord operator and seeds its cursor
from that channel's newest message, so deployment never answers old conversation history.
Each pass fetches at most 50 newer messages. Replies are split at 1,900 characters, and a
Discord `429` sleeps for the supplied `retry_after` before one bounded retry. Messages from
unlisted users, non-private conversations, and other bots receive silence and produce the
same bounded `chat_message_dropped` evidence as Telegram.

There is one REST-specific limit: Discord exposes no endpoint that lists every inbound DM
channel. Steward opens only the configured operators' channels, so a new unknown account's
DM is silent but cannot produce a drop event because the REST poller never observes it. The
bridge still records `not an operator` for any untrusted message a transport does hand it;
discovering arbitrary unknown-account DMs belongs to Gateway ingestion.

`steward chat list` authenticates each configured Discord token with `GET /users/@me` and
prints the discovered handle, for example `pip/discord: discord:pip — reachable, bot @Pip`.
That makes a token copied from the wrong resident's application visible before the daemon
starts answering.

## Discord room posts

A Discord chat route may add `posts_to: [household, announcements]`. These are readable
channel names, not ids; steward resolves them against `STEWARD_CHAT_DISCORD_GUILD` and
refuses unknown names. Empty or absent means no room posting permission.

Any completed session may request a post from its final machine-read action region:

```text
<discord post channel="household">{"text": "Morning summary…"}</discord>
```

Steward holds the bot token, redacts then caps each message, and attempts at most five
posts per session. Success emits `chat_message_posted` with the bounded length but no text.
A malformed, disallowed, unknown, or failed post emits `chat_post_refused` and raises a
`needs_human` under `rejected_post`; the transcript records the outcome. This is the
outbound counterpart to the action harvesting described in [delegation.md](delegation.md).

## Future Discord gateway (design only)

This is the design for [warren#424](https://github.com/0xCommanderKeen/warren/issues/424),
not a promise that the gateway is enabled. It becomes useful only after the Discord REST
transport, room posting, grants and guild mirror have had time to settle. The
`ChatTransport` boundary remains the boundary: each Discord bot gets one gateway worker
thread inside the existing chat daemon, and `poll()` drains that worker's bounded queue.
Nothing that fires a resident session learns about WebSockets.

### Dependency and ownership

Use [`discord.py`](https://discordpy.readthedocs.io/en/stable/) rather than building the
Gateway protocol directly on `websockets`. Discord requires more than a WebSocket: Hello
and jittered heartbeats, heartbeat acknowledgements, sequence tracking, Resume versus
Identify, close-code handling, session-start limits, intents, and dispatch decoding.
`discord.py` already owns that state machine and reconnects its client; a direct
`websockets` implementation would make Warren own and test all of it for no product
advantage. Pin the dependency in the **control-plane** package and image only. Resident
images never import it and never receive a bot token.

`DiscordGatewayWorker` owns one `discord.Client` and event loop in one named daemon thread
per active bot token. It translates only the dispatches Warren understands into small
transport-neutral records and puts them into a bounded `queue.Queue`; callbacks never run
a resident session. `DiscordTransport.poll()` drains that queue, preserves the existing
freshness, operator, bot-author and busy-resident checks, and returns the same `Message`
shape as REST polling. Queue overflow is a visible diagnostic and switches that bot to
the REST catch-up path; it must never discard events silently.

The declared intents are `GUILDS`, `GUILD_MESSAGES`, `DIRECT_MESSAGES`, and
`MESSAGE_CONTENT`; add `GUILD_MEMBERS` only for the member-event feature. Message Content
and Guild Members are privileged intents and must be enabled in each resident
application's Developer Portal settings before that feature is declared active. Presence
here means the bot's own connection status, not reading other members' presence, so
`GUILD_PRESENCES` is not requested.

### Connect, heartbeat, resume, and degradation

`discord.py` owns protocol heartbeats and Resume. The Warren wrapper observes ready,
disconnect, resume and terminal-error callbacks and publishes a per-bot health snapshot:
`connecting`, `ready`, `degraded`, or `stopped`, plus the last dispatch, heartbeat and
error times. A missed heartbeat acknowledgement, Discord's Reconnect dispatch, or a
resumable close starts an exponential-backoff reconnect with jitter and the library's
saved session and sequence. A non-resumable close or Invalid Session re-identifies only
after the library's backoff; configuration close codes (bad token, disallowed intent,
sharding required) stop that bot and make `steward chat list` name the fault rather than
burning the Identify allowance.

The REST cursor remains authoritative per known channel even while the socket is healthy.
A gateway dispatch is first inserted into a small durable ingress spool in the shared
state database; that insert and the channel cursor advance are one transaction. The
thread's queue contains only a wake-up/reference to that row, and `poll()` drains the
spool, so a full queue or process crash cannot strand a message behind its cursor. The row
is marked handled only after the bridge accepts or deliberately drops it. Message
snowflakes are unique in the spool, which also deduplicates Resume replay.

While a worker is not `ready`, the existing daemon pass polls each operator DM and each
configured guild channel with `GET /channels/{id}/messages?after=<cursor>`, oldest first,
and writes the same spool transaction. On a fresh Identify, REST closes the gap before the
worker is marked ready. This makes DMs and guild mentions slower during an outage, not
silent, without firing one message twice.

REST cannot reproduce every gateway feature. During degradation the bot appears offline;
presence returns with Ready. Member joins fall back to the existing 15-minute guild-member
mirror comparison. Discord offers neither an endpoint that lists component interactions
nor a second interaction delivery mode while Gateway delivery is selected, so approval
buttons are disabled on detection of degradation and their accompanying message points at
the ordinary Townhall/API approval path. These limitations are explicit health, not
pretend parity. If REST itself fails, the normal durable event fallback records the error
and `steward chat list` reports the last successful ingress; no inbound message is claimed
as handled until its cursor commit.

The bot is online whenever its worker is Ready. When a gateway-originated DM or mention
has passed admission and acquired the resident claim, the bridge triggers Discord's
typing endpoint immediately and every eight seconds until the session finishes or loses
its claim (Discord typing expires after ten seconds). Typing failures are logged and do
not alter the run. REST-fallback messages use the same typing lease.

### Guild mentions and member joins

A guild message becomes inbound chat only when it is in the configured guild and an
allowlisted channel, explicitly mentions that resident bot, is authored by a configured
human operator, and is not authored by any bot. Strip only the bot mention used for
routing; the remaining bounded text becomes the task. The conversation key includes
guild and channel, so room context cannot bleed into a DM or another room. No ambient
channel message starts a session. This deliberately enables Message Content even though
Discord currently exempts messages that mention the app: #424 requires the intent, and
declaring it makes Portal configuration explicit instead of making routing depend on a
policy exception. A missing Portal toggle fails visibly in preflight.

`GUILD_MEMBER_ADD` replaces Herald's periodic discovery when the worker is healthy. It
writes the same guild-mirror member shape and the same durable "welcome pending" fact the
poller writes; Herald's routine remains the sole consumer and its journal/idempotency rule
still owns "exactly one welcome." Resume replay and REST comparison therefore converge on
one member identity instead of creating two welcomes.

### Approval buttons belong to approvals

Buttons are an **approvals feature with a Discord adapter**, not chat messages. The
notification/approval publisher may render an Approve or Deny component, but a click
never enters `ChatBridge`, never starts a resident, and never becomes transcript text. It
calls the same `ApprovalTransitions.decide` path as `POST /approvals/{id}` so expiry,
offered decisions, atomic first-writer-wins behavior, resume effects, events and auditing
have one owner.

The component `custom_id` is an opaque, random, single-use nonce. Steward stores its hash
against the approval request, offered decision, Discord application, guild/channel and
message; the button carries no request authority or operator name. An
`INTERACTION_CREATE` received on that bot's authenticated Gateway session supplies the
pressing Discord user (`member.user.id` in a guild, `user.id` in a DM). Steward maps
`discord:<id>` to a named configured operator, verifies the stored application and message
binding, reloads the still-pending approval, and then passes that operator name to
`ApprovalTransitions` as `decided_by`. Unknown users get an ephemeral refusal; expired,
already-decided, mismatched, or replayed nonces have no transition and refresh the message
to its terminal state.

Gateway interactions are authenticated by the bot's TLS Gateway session; unlike outgoing
interaction webhooks, they are not individually Ed25519-signed HTTP requests. The pressing
user id is nevertheless Discord-supplied rather than message text. Warren acknowledges or
defers the interaction inside Discord's response deadline before doing effects, disables
both buttons after the atomic decision, and retains the ordinary approval URL/text as the
degraded and accessibility path. A later implementation must threat-model token theft,
nonce replay, operator revocation between publication and click, and two operators clicking
at once before it is allowed to ship.

## Explicitly not in v0

- **`needs_human` and task completions pushed into the chat.** Those are *notifications* —
  one-way, nothing listens for a reply — and the Discord notification transport sends those
  knocks through the fleet webhook ([warren#418](../src/steward/notify.py)). The bridge itself only ever speaks when
  spoken to; a delivered routine is the scheduler speaking, through the bridge's egress.
- **Approval buttons.** An approval is an authorisation, and "who pressed it" is a security
  question a v0 chat channel has no honest answer to.
- **Editing, reactions, photos, voice notes.** `allowed_updates: ["message"]`, text only;
  anything else is ignored rather than half-answered.
- **A conversation the resident starts.** A delivered routine is a message the *manifest*
  scheduled, not one the resident decided to send; nothing a session says mid-run reaches
  a phone, and there is no API a session can call to send one.

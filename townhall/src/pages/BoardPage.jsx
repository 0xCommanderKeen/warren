/* The job board: work nobody has been told to do yet.
 *
 * Posting puts a task in steward's store and announces it. Dispatch is **pull-based** — no
 * resident is prompted, and `task_claimed` in the village's log is the only proof one picked
 * it up. The form below says so, and the ticket it raises is confirmed by finding the task
 * on the board steward keeps rather than by the 202 that accepted it.
 */

import { useState } from "react";
import { Link } from "../navigation.jsx";
import { routeTo } from "../routes.js";
import { useSteward, useStewardQuery } from "../steward/context.jsx";
import { Gate } from "../console/Gate.jsx";
import { confirmJob, useLedger } from "../console/ledger.jsx";
import {
  Actions, Button, Check, Clock, Empty, Field, Input, Loading, Note, PageHead, Panel, Problem,
  Row, Rows, Section, Stack, Swatch, Tag, Textarea,
} from "../console/ui.jsx";
import { stamp } from "../console/time.js";

const GROUPS = [
  ["open", "Open", "Posted and unclaimed. A resident claims one on its own next wake-up; steward prompts nobody."],
  ["claimed", "Claimed", "Held under a lease. If the lease expires before the claimant finishes, the task fails and returns."],
  ["done", "Done", "The claimant said so and named its artifacts."],
  ["failed", "Failed", "The claimant gave up, or its lease ran out."],
];

const JOB_COLUMNS = "1.6fr 1fr .9fr 1fr";

function PostForm({ skills, onSettled }) {
  const { client } = useSteward();
  const { raise } = useLedger();
  const [title, setTitle] = useState("");
  const [detail, setDetail] = useState("");
  const [required, setRequired] = useState([]);
  const [complaint, setComplaint] = useState(null);
  const [refusal, setRefusal] = useState(null);
  const [sending, setSending] = useState(false);

  async function post(event) {
    event.preventDefault();
    setRefusal(null);
    if (!title.trim()) {
      setComplaint("A task needs a title. Nothing was sent.");
      return;
    }
    setComplaint(null);
    setSending(true);
    try {
      const answer = await client.postJob({
        title: title.trim(),
        detail: detail.trim(),
        required_skills: required,
      });
      raise({
        what: `post "${title.trim()}"`,
        requestId: answer.request_id,
        why: answer.message,
        confirm: confirmJob(client, answer.task_id),
        onSettled,
      });
      setTitle("");
      setDetail("");
      setRequired([]);
    } catch (caught) {
      setRefusal(caught);
    } finally {
      setSending(false);
    }
  }

  return (
    <form onSubmit={post}>
      <Panel title="Post a task">
        <Field
          label="title"
          hint="One line naming the work."
          problems={complaint ? [{ problem: complaint, severity: "error" }] : []}
        >
          <Input
            value={title}
            placeholder="Research X"
            invalid={Boolean(complaint)}
            onChange={(event) => {
              setTitle(event.target.value);
              setComplaint(null);
            }}
          />
        </Field>
        <Field label="detail" hint="Everything the claimant needs to know.">
          <Textarea rows={4} value={detail} onChange={(event) => setDetail(event.target.value)} />
        </Field>
        <Field
          label="required skills"
          hint="A resident may only claim this if its effective skills cover every one of these."
        >
          <div className="mt-2">
            {skills.length ? (
              skills.map((skill) => (
                <Check
                  key={skill.name}
                  name={skill.name}
                  description={skill.description}
                  checked={required.includes(skill.name)}
                  onChange={(event) =>
                    setRequired((current) =>
                      event.target.checked
                        ? [...current, skill.name]
                        : current.filter((name) => name !== skill.name),
                    )
                  }
                />
              ))
            ) : (
              <p className="m-0 text-[11.5px] text-faint">
                The library is empty, so nothing can be required.
              </p>
            )}
          </div>
        </Field>
        {refusal ? <Problem error={refusal} /> : null}
        <Actions>
          <Button tone="primary" type="submit" disabled={sending}>
            {sending ? "asking steward…" : "Post to the board"}
          </Button>
          <Note>Announced as task_posted. Nobody is prompted.</Note>
        </Actions>
      </Panel>
    </form>
  );
}

function JobRow({ job, souls }) {
  const claimant = job.claimant || job.assignee;
  const soul = claimant ? souls.get(claimant) : null;
  return (
    <Row columns={JOB_COLUMNS} accent={soul?.accent}>
      <Stack sub={job.detail || "no detail given"}>
        <span className="font-serif text-[16px] leading-[1.3]">{job.title}</span>
      </Stack>
      <span>
        {(job.required_skills || []).length ? (
          job.required_skills.map((name) => <Tag key={name}>{name}</Tag>)
        ) : (
          <span className="text-faint">no skills required</span>
        )}
      </span>
      {claimant ? (
        <Link to={routeTo.resident(claimant)} className="text-inherit no-underline">
          <span className="flex min-w-0 items-baseline gap-2.5">
            <Swatch accent={soul?.accent} className="-translate-y-px" />
            <span className="min-w-0">
              <span className="block truncate">{soul ? soul.name : claimant}</span>
              <span className="text-[11px] text-faint">
                {job.assignee ? "delegated" : "claimed"}
              </span>
            </span>
          </span>
        </Link>
      ) : (
        <span className="text-faint">posted by {job.posted_by}</span>
      )}
      <Stack
        sub={
          job.status === "claimed" && job.lease_expires_at ? (
            <>
              lease <Clock at={job.lease_expires_at} mode="until" />
            </>
          ) : (
            job.outcome || job.reason || `posted ${stamp(job.created_at)}`
          )
        }
      >
        <Clock at={job.finished_at || job.claimed_at || job.created_at} />
      </Stack>
    </Row>
  );
}

function Board() {
  const { client } = useSteward();
  const { data, error, loading, refresh } = useStewardQuery(
    (signal) =>
      Promise.all([
        client.listJobs({ signal }),
        client.listSkills({ signal }),
        client.listResidents({ signal }),
      ]).then(([board, library, listing]) => ({
        jobs: board.jobs || [],
        skills: library.skills || [],
        souls: new Map((listing.residents || []).map((item) => [item.id, item.soul])),
      })),
    [],
  );

  if (loading && !data) return <Loading>reading the board…</Loading>;
  if (error) return <Problem error={error} />;

  return (
    <>
      <PostForm skills={data.skills} onSettled={refresh} />
      {GROUPS.map(([status, title, why]) => {
        const group = data.jobs.filter((job) => job.status === status);
        return (
          <div key={status}>
            <Section count={group.length}>{title}</Section>
            {group.length ? (
              <Rows>
                {group.map((job) => (
                  <JobRow key={job.task_id} job={job} souls={data.souls} />
                ))}
              </Rows>
            ) : (
              <Empty title={`No ${status} tasks.`}>{why}</Empty>
            )}
          </div>
        );
      })}
    </>
  );
}

export default function BoardPage() {
  const { locked } = useSteward();
  return (
    <>
      <PageHead title="Job board">
        Work nobody has been told to do yet. Posting puts a task in steward's store and
        announces it; dispatch is pull-based, so no resident is prompted and{" "}
        <strong className="text-ink">task_claimed</strong> in the village's log is the only
        proof one picked it up.
      </PageHead>
      {locked ? <Gate what="The job board" /> : <Board />}
    </>
  );
}

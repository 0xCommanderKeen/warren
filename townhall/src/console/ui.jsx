/* The steward console's vocabulary, on this stack.
 *
 * Every piece here is a translation of a rule in `steward/ui/app.css` into Tailwind
 * utilities — the same hairlines, the same uppercase micro-labels, the same one meaning
 * per colour. warren#225 decided this is a port and not a redesign, so where this file and
 * that stylesheet disagree, that stylesheet is right. The single intentional divergence is
 * the contrast correction documented at the top of `styles.css` (#152).
 */

import { useEffect, useState } from "react";
import { isTime, stamp, words } from "./time.js";

const cx = (...parts) => parts.filter(Boolean).join(" ");

/* -- time ---------------------------------------------------------------------------- */

/**
 * One second, shared.
 *
 * The console rewrote every `<time>` on the page from a single `setInterval`, and this is
 * the same trick: one timer for the whole app, however many clocks are mounted. A clock
 * per interval would be N timers whose only job is to disagree about what "now" is.
 */
export function useNow(everyMs = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), everyMs);
    return () => clearInterval(timer);
  }, [everyMs]);
  return now;
}

/**
 * A relative time that keeps itself honest — rewritten every second, forever.
 *
 * `mode="until"` counts down to a deadline and then says it has passed, rather than
 * turning into a count-up that reads as though the thing is still coming.
 */
export function Clock({ at, mode = "ago", className }) {
  const now = useNow();
  if (!at) return <span className={cx("text-faint", className)}>—</span>;
  const gone = mode === "until" && isTime(at) && Date.parse(at) <= now;
  return (
    <time dateTime={at} title={stamp(at) || undefined} className={cx(gone && "text-faint", className)}>
      {words(at, { mode, now })}
    </time>
  );
}

/* -- headings ------------------------------------------------------------------------ */

export function PageHead({ title, children, aside }) {
  return (
    <div className="rise mb-[26px] flex flex-wrap items-start justify-between gap-6">
      <div className="min-w-0">
        <h1 className="m-0 font-serif text-[34px] font-normal leading-[1.1] tracking-[.005em]">{title}</h1>
        {children ? (
          <p className="mt-3 max-w-[74ch] text-[12.5px] leading-[1.7] text-dim">{children}</p>
        ) : null}
      </div>
      {aside ? <div className="flex shrink-0 flex-wrap items-center gap-2">{aside}</div> : null}
    </div>
  );
}

/**
 * The head of a detail page: a way back, a name in this resident's own colour, a subtitle.
 *
 * The accent is a CSS custom property on the element that consumes it, which is the whole
 * of the #151 fix — the console meant to do exactly this and its DOM helper silently
 * dropped it. A resident with no accent falls back to ember *by declaration*, not by
 * accident.
 */
export function DetailHead({ accent, title, back, children, aside }) {
  return (
    <div
      className="rise mb-[26px] flex flex-wrap items-start justify-between gap-6"
      style={accent ? { "--accent": accent } : undefined}
    >
      <div className="min-w-0">
        {back}
        <h1 className="m-0 mt-2 border-l-[3px] border-l-[color:var(--accent,var(--color-ember))] pl-3.5 font-serif text-[34px] font-normal leading-[1.1]">
          {title}
        </h1>
        {children ? (
          <p className="mt-3 max-w-[74ch] pl-3.5 text-[12.5px] leading-[1.7] text-dim">{children}</p>
        ) : null}
      </div>
      {aside ? <div className="flex shrink-0 flex-wrap items-center gap-2">{aside}</div> : null}
    </div>
  );
}

export function Section({ children, count }) {
  return (
    <h2 className="section-rule mt-[34px] mb-[14px] flex items-center gap-3 font-mono text-[11px] font-normal uppercase leading-none tracking-[.2em] text-dim">
      {children}
      {count === undefined ? null : <span className="tracking-[.1em] text-faint">({count})</span>}
    </h2>
  );
}

export const Label = ({ children, className }) => (
  <span className={cx("text-[9.5px] uppercase tracking-[.2em] text-faint", className)}>{children}</span>
);

export const Rule = () => <hr className="my-6 border-0 border-t border-rule" />;

/* -- panels and facts ---------------------------------------------------------------- */

export function Panel({ title, children, className, tone }) {
  return (
    <section
      className={cx(
        "mb-4 border bg-deep px-[22px] py-5",
        tone === "ember" ? "border-ember/40" : "border-rule",
        className,
      )}
    >
      {title ? (
        <h3 className="m-0 mb-[14px] font-mono text-[11px] font-normal uppercase leading-none tracking-[.2em] text-dim">
          {title}
        </h3>
      ) : null}
      {children}
    </section>
  );
}

/** `pairs` is [term, value]; a null or empty value is left out rather than shown blank. */
export function Facts({ pairs, className }) {
  const shown = pairs.filter(([, value]) => value !== null && value !== undefined && value !== "");
  if (!shown.length) return null;
  return (
    <dl className={cx("m-0 grid grid-cols-[max-content_1fr] gap-x-[18px] gap-y-[7px]", className)}>
      {shown.map(([term, value]) => (
        <div className="contents" key={term}>
          <dt className="pt-[3px] text-[9.5px] uppercase tracking-[.17em] text-faint">{term}</dt>
          <dd className="m-0 [overflow-wrap:anywhere]">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/* -- badges and tags ----------------------------------------------------------------- */

const BADGE_TONE = {
  "": "text-dim border-rule",
  on: "text-ink border-rule-lit",
  wait: "text-wait border-wait/45",
  live: "text-live border-live/40",
  fail: "text-fail border-fail/50",
  ember: "text-ember border-ember/45",
};

export function Badge({ tone = "", children, title }) {
  return (
    <span
      title={title}
      className={cx(
        "inline-flex items-center gap-1.5 whitespace-nowrap border px-[7px] py-[2px] text-[10px] uppercase tracking-[.1em]",
        BADGE_TONE[tone] || BADGE_TONE[""],
      )}
    >
      <span className="size-[5px] bg-current" />
      {children}
    </span>
  );
}

export const Badges = ({ children, className }) => (
  <span className={cx("flex flex-wrap items-center gap-1.5", className)}>{children}</span>
);

export const Tag = ({ children, tone, title }) => (
  <span
    title={title}
    className={cx(
      "mb-1 mr-1 inline-block border bg-deeper px-[7px] py-px text-[10.5px]",
      tone === "default" ? "border-ember/30 text-ember" : "border-rule-2 text-dim",
    )}
  >
    {children}
  </span>
);

/* -- absence, refusal, waiting ------------------------------------------------------- */

export function Empty({ title, children }) {
  return (
    <div className="border border-dashed border-rule bg-ink/[.012] px-6 py-[26px] text-dim">
      <strong className="mb-2 block font-serif text-[18px] font-normal text-ink">{title}</strong>
      <p className="m-0 max-w-[70ch] text-[12px] leading-[1.7]">{children}</p>
    </div>
  );
}

export const Loading = ({ children = "reading…" }) => (
  <p className="py-10 text-faint" aria-live="polite">
    {children}
  </p>
);

export const Verbatim = ({ value, summary = "the response, verbatim" }) => (
  <details className="mt-[11px]">
    <summary className="cursor-pointer text-[10.5px] uppercase tracking-[.14em] text-dim">{summary}</summary>
    <pre className="mt-[9px] overflow-x-auto border border-rule-2 bg-void p-[11px] text-[11px] text-dim">
      {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
    </pre>
  </details>
);

/**
 * A refusal, rendered as steward gave it.
 *
 * The status and the error code steward chose, its own message, its structured
 * diagnostics, and the raw response behind a disclosure. Nothing is rephrased into
 * friendlier words: the code is the thing an operator greps for.
 */
export function Problem({ error, title }) {
  if (!error) return null;
  const code = error.status ? `${error.status} · ${error.code}` : error.code || "console fault";
  return (
    <div className="my-[14px] border border-l-[3px] border-fail/45 bg-fail/[.06] px-[18px] py-[15px]">
      <div className="text-[10px] uppercase tracking-[.16em] text-fail">{title || code}</div>
      <div className="mt-[7px] whitespace-pre-wrap [overflow-wrap:anywhere]">{error.message}</div>
      {error.diagnostics?.length ? <Diagnostics items={error.diagnostics} /> : null}
      {error.raw ? <Verbatim value={error.raw} /> : null}
    </div>
  );
}

/** steward#214's `file`/`field`/`problem`/`example`/`severity`, laid out to be acted on. */
export function Diagnostics({ items }) {
  if (!items?.length) return null;
  return (
    <ul className="mt-3 list-none space-y-2 p-0">
      {items.map((item, index) => (
        <li
          key={`${item.field}:${index}`}
          className={cx(
            "border-l-2 pl-3",
            item.severity === "warning" ? "border-wait" : "border-fail",
          )}
        >
          <div className="text-[10px] uppercase tracking-[.14em]">
            <span className={item.severity === "warning" ? "text-wait" : "text-fail"}>
              {item.field || item.file || "the tree"}
            </span>
            {item.file && item.field ? <span className="ml-2 text-faint">{item.file}</span> : null}
          </div>
          <div className="mt-1 text-[12px] leading-[1.6] [overflow-wrap:anywhere]">{item.problem}</div>
          {item.example ? (
            <pre className="mt-1.5 overflow-x-auto border border-rule-2 bg-void p-2 text-[11px] text-dim">
              {item.example}
            </pre>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

/* -- forms --------------------------------------------------------------------------- */

const CONTROL =
  "w-full rounded-none border bg-void px-[10px] py-2 font-mono text-[13px] leading-[1.5] text-ink transition-colors hover:border-rule-lit focus:border-ember focus:outline-none";

export function Field({ label, hint, problems = [], children, id }) {
  const bad = problems.some((item) => item.severity !== "warning");
  return (
    <label className="mb-[15px] block" htmlFor={id}>
      <Label className="mb-1.5 block">{label}</Label>
      {children}
      {hint ? <span className="mt-1.5 block text-[10.5px] leading-[1.6] text-faint">{hint}</span> : null}
      {problems.map((item, index) => (
        <span
          key={index}
          className={cx("mt-1.5 block text-[11px]", bad ? "text-fail" : "text-wait")}
        >
          {item.problem}
          {item.example ? <span className="mt-1 block text-faint">e.g. {item.example}</span> : null}
        </span>
      ))}
    </label>
  );
}

export const Input = ({ invalid, className, ...props }) => (
  <input
    {...props}
    aria-invalid={invalid || undefined}
    className={cx(CONTROL, invalid && "border-fail", className)}
  />
);

export const Textarea = ({ invalid, className, rows = 4, ...props }) => (
  <textarea
    {...props}
    rows={rows}
    aria-invalid={invalid || undefined}
    className={cx(CONTROL, "min-h-[78px] resize-y", invalid && "border-fail", className)}
  />
);

export const Select = ({ className, children, ...props }) => (
  <select {...props} className={cx(CONTROL, className)}>
    {children}
  </select>
);

const BUTTON_TONE = {
  primary: "bg-ember border-ember text-on-ember hover:bg-ember-lit hover:border-ember-lit",
  ghost: "bg-transparent border-rule text-dim hover:text-ink hover:border-rule-lit",
  danger: "bg-transparent border-fail/45 text-fail hover:bg-fail/10",
};

/** The button look, on its own, for the links that have to look like buttons. */
export const buttonClass = (tone = "ghost", tiny) =>
  cx(
    "inline-flex cursor-pointer items-center rounded-none border font-mono uppercase leading-none tracking-[.16em] no-underline transition-colors disabled:cursor-not-allowed disabled:opacity-40",
    tiny ? "px-2.5 py-[5px] text-[10px] tracking-[.12em]" : "px-4 py-[9px] text-[11px]",
    BUTTON_TONE[tone],
  );

export function Button({ tone = "ghost", tiny, className, ...props }) {
  return <button type="button" {...props} className={cx(buttonClass(tone, tiny), className)} />;
}

export const Actions = ({ children, className }) => (
  <div className={cx("flex flex-wrap items-center gap-[9px]", className)}>{children}</div>
);

export const Note = ({ children }) => <span className="text-[10.5px] text-faint">{children}</span>;

/* -- rows ---------------------------------------------------------------------------- */

export const Rows = ({ children, className }) => (
  <div className={cx("border-t border-rule", className)}>{children}</div>
);

export function Row({ columns, children, head, href, onClick, accent, className }) {
  const shared = cx(
    "grid items-center gap-[18px] border-b border-l-2 border-l-transparent py-[13px] pl-4 pr-[14px] text-left transition-colors",
    head
      ? "border-b-rule pt-0 pb-[9px] text-[9.5px] uppercase tracking-[.18em] text-faint"
      : "border-b-rule-2",
    (href || onClick) && "hover:bg-ink/[.03] hover:border-l-[color:var(--row-accent,var(--color-ember))]",
    className,
  );
  const style = { gridTemplateColumns: columns, ...(accent ? { "--row-accent": accent } : {}) };
  if (href) {
    return (
      <a href={href} onClick={onClick} className={cx(shared, "no-underline text-inherit")} style={style}>
        {children}
      </a>
    );
  }
  if (onClick) {
    return (
      <button type="button" onClick={onClick} className={cx(shared, "w-full")} style={style}>
        {children}
      </button>
    );
  }
  return (
    <div className={shared} style={style}>
      {children}
    </div>
  );
}

/** The name-and-role cluster the console repeats in every list of residents. */
export function Who({ accent, name, id, role, retired }) {
  return (
    <span className="flex min-w-0 items-baseline gap-2.5">
      <Swatch accent={accent} className="-translate-y-px" />
      <span className="min-w-0">
        <span className="font-serif text-[17px] leading-[1.2]">{name}</span>{" "}
        <span className="text-[11px] text-faint">{id}</span>
        {retired ? <> <Badge tone="fail">retired</Badge></> : null}
        {role ? <span className="mt-[3px] block text-[11px] text-dim">{role}</span> : null}
      </span>
    </span>
  );
}

/**
 * A resident's declared colour, as a colour (#151).
 *
 * The console passed its accent through `Object.assign(node.style, …)`, which cannot set a
 * CSS custom property — so `--accent` never applied, every hover border and detail header
 * fell back to the generic ember, and because the fallback looked fine nobody noticed. A
 * React style object *does* support `"--accent"` keys, and `Row` and `Detail` below feed
 * theirs through one, so the declared colour is the colour on the screen.
 */
export const Swatch = ({ accent, className }) => (
  <span
    className={cx("size-2 flex-none", className)}
    style={{ background: accent || "var(--color-ember)" }}
  />
);

/** A checkbox with a name and a description, the shape the console's grant lists used. */
export function Check({ name, description, note, disabled, ...props }) {
  return (
    <label
      className={cx(
        "mb-2 flex cursor-pointer items-baseline gap-2.5 border border-rule-2 bg-deeper px-3 py-2.5",
        disabled && "cursor-not-allowed opacity-70",
      )}
    >
      <input type="checkbox" disabled={disabled} {...props} className="mt-0.5 accent-ember" />
      <span className="min-w-0">
        <span className="block text-ink">
          {name}
          {note ? <> {note}</> : null}
        </span>
        {description ? (
          <span className="mt-0.5 block text-[11px] leading-[1.55] text-dim">{description}</span>
        ) : null}
      </span>
    </label>
  );
}

export const Stack = ({ children, sub }) => (
  <span className="flex min-w-0 flex-col gap-[3px]">
    {children}
    {sub === null || sub === undefined ? null : <span className="text-[10.5px] text-faint">{sub}</span>}
  </span>
);

/* -- the budget gauge ---------------------------------------------------------------- */

/**
 * Spent against the worst-off cap.
 *
 * Straight from the console: a resident with no declared budget shows its summary rather
 * than an empty bar, because "unlimited" and "unknown" must not look the same.
 */
export function Gauge({ budget, className }) {
  if (!budget || budget.declared === false) {
    return (
      <Stack sub={`${budget?.runs || 0} runs counted`}>
        <span className="text-faint">{budget?.summary || "no limit"}</span>
      </Stack>
    );
  }
  const worst = (budget.budgets || [])
    .filter((item) => item.limit)
    .map((item) => item.spent / item.limit)
    .reduce((left, right) => Math.max(left, right), 0);
  const hot = budget.paused || worst >= 1;
  return (
    <span className={cx("w-full max-w-[190px]", className)}>
      <span className="relative block h-[3px] bg-ink/10">
        <span
          className={cx("absolute inset-y-0 left-0", hot ? "bg-fail" : worst >= 0.7 ? "bg-wait" : "bg-live")}
          style={{ right: `${Math.max(0, 100 - Math.min(worst, 1) * 100)}%` }}
        />
      </span>
      <span className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[10.5px] text-dim">
        {budget.paused ? <Badge tone="fail">paused</Badge> : null}
        {budget.summary}
      </span>
    </span>
  );
}

/* -- what steward said --------------------------------------------------------------- */

/**
 * The receipt for one write.
 *
 * The console's pending ledger exists because steward's *action* endpoints answer
 * "accepted" and only its request log can say "done" — so the console polls. The write
 * endpoints in this surface are not those: `PUT /residents/{id}/declaration` validates,
 * writes and commits before it answers, so its answer already IS the outcome and there is
 * nothing honest left to poll for. What carries over is the rule, not the machinery:
 * this shows steward's own word, and the commit steward reported making.
 */
export function Receipt({ title, status, commit, children, onDismiss }) {
  const state = commit?.state;
  return (
    <div
      className={cx(
        "my-[14px] border border-l-[3px] px-[18px] py-[15px]",
        state === "committed" ? "border-live/45 bg-live/[.05]" : "border-wait/45 bg-wait/[.05]",
      )}
    >
      <div className="flex items-baseline justify-between gap-3">
        <div className={cx("text-[10px] uppercase tracking-[.16em]", state === "committed" ? "text-live" : "text-wait")}>
          {title}
          {status ? <span className="ml-2 text-ink">{status}</span> : null}
        </div>
        {onDismiss ? (
          <button
            type="button"
            onClick={onDismiss}
            title="dismiss"
            className="cursor-pointer border-0 bg-transparent px-0 pl-2 text-[13px] text-faint hover:text-ink"
          >
            ×
          </button>
        ) : null}
      </div>
      {commit ? (
        <div className="mt-2.5">
          {commit.state === "committed" ? (
            <Facts
              pairs={[
                ["commit", <code className="text-live">{commit.short}</code>],
                ["subject", commit.message],
                ["note", commit.note],
              ]}
            />
          ) : (
            <p className="m-0 text-[12px] leading-[1.6] text-dim">
              {commit.note ||
                "steward committed nothing: what is on disk was already what is in git. That is the converged answer, not a failure."}
            </p>
          )}
        </div>
      ) : null}
      {children ? <div className="mt-2.5 text-[12px] leading-[1.6] text-dim">{children}</div> : null}
    </div>
  );
}

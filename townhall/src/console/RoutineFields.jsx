import { linesToList, listToLines, numberValue, scalarValue, setIn } from "../manifest.js";
import { Button, Field, Input, Note, Panel, Textarea } from "./ui.jsx";

// Steward spells list positions with brackets; accept dotted positions as well.
function routineProblems(diagnostics, path) {
  return diagnostics.filter((item) => {
    const field = item.field?.replace(/\[(\d+)\]/g, ".$1");
    return field === path || field?.startsWith(`${path}.`);
  });
}

export function RoutineFields({ manifest, edit, diagnostics = [] }) {
  const routines = Array.isArray(manifest.routines) ? manifest.routines : [];
  const change = (index, key, value) => edit("routines", routines.map((routine, row) => (
    row === index ? setIn(routine, key, value) : routine
  )));

  return (
    <Panel title="Routines">
      <p className="mt-0 mb-4"><Note>
        Standing work, scheduled in each routine's time zone. Write the declaration to save
        changes; the receipt offers a reload of steward's own copy.
      </Note></p>
      {routines.length ? null : <p className="text-[12px] text-dim">No routines declared.</p>}
      {routines.map((routine, index) => {
        const field = (key, hint, { multiline = false, numeric = false, list = false } = {}) => {
          const problems = routineProblems(diagnostics, `routines.${index}.${key}`);
          const Control = multiline ? Textarea : Input;
          return (
            <Field label={key} hint={hint} problems={problems}>
              <Control
                aria-label={`routine ${index + 1} · ${key}`}
                value={list ? listToLines(routine[key]) : scalarValue(routine[key])}
                invalid={problems.length > 0}
                {...(numeric ? { inputMode: "numeric" } : {})}
                onChange={(event) => {
                  const value = event.target.value;
                  let next = value || undefined;
                  if (list) next = linesToList(value);
                  if (numeric) {
                    const parsed = numberValue(value);
                    next = parsed === null ? value : parsed;
                  }
                  change(index, key, next);
                }}
              />
            </Field>
          );
        };
        const enabledProblems = routineProblems(diagnostics, `routines.${index}.enabled`);
        return (
          <fieldset key={index} className="mb-5 min-w-0 border border-rule-2 p-4">
            <legend className="px-2 font-serif text-[16px] text-ink">Routine {index + 1}</legend>
            <div className="grid gap-x-4 [grid-template-columns:repeat(auto-fit,minmax(min(100%,190px),1fr))]">
              {field("id", "Unique within this resident. Lowercase letters, digits and hyphens.")}
              {field("schedule", "Five-field cron, for example 0 9 * * * for every day at 09:00.")}
              {field("schedule_tz", "IANA time zone, for example Europe/Ljubljana. Blank means UTC.")}
            </div>
            {field("prompt", "What the resident should do when this routine fires.", { multiline: true })}
            <div className="grid gap-x-4 [grid-template-columns:repeat(auto-fit,minmax(min(100%,190px),1fr))]">
              {field("requires", "Required skills, one per line. Must be granted or inherited.", { multiline: true, list: true })}
              <div>
                {field("timeout_s", "Required: maximum run time in seconds, a positive whole number.", { numeric: true })}
                <Field label="enabled" problems={enabledProblems}>
                  <input
                    type="checkbox"
                    aria-label={`routine ${index + 1} · enabled`}
                    className="accent-ember"
                    checked={routine.enabled !== false}
                    aria-invalid={enabledProblems.length > 0 || undefined}
                    onChange={(event) => change(index, "enabled", event.target.checked)}
                  />
                </Field>
              </div>
            </div>
            <Button tiny tone="ghost" aria-label={`Remove routine ${index + 1}`}
              onClick={() => edit("routines", routines.filter((_routine, row) => row !== index))}>
              Remove routine
            </Button>
          </fieldset>
        );
      })}
      <Button tiny tone="ghost" onClick={() => edit("routines", [
        ...routines, { id: "", schedule: "", prompt: "", timeout_s: 900 },
      ])}>
        Add routine
      </Button>
    </Panel>
  );
}

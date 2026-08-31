/* The write path's front door.
 *
 * The steward console asks for a token before it can draw anything, because every one of
 * its reads is gated too. Townhall's reads are not: they come from Chronicle's public
 * `/state`, and gating the fleet page behind a token nobody needs to watch the fleet would
 * be a regression. So the gate is inline on the pages that write, and the fleet keeps
 * working for somebody who never opens it.
 *
 * What is carried over intact is the honesty: the token goes in this tab's sessionStorage
 * and nowhere else, "this server runs open" is a thing a person says out loud rather than
 * something inferred from a 200, and "forget" is one click away in the rail.
 */

import { useState } from "react";
import { Actions, Button, Field, Input, Note, Panel } from "./ui.jsx";
import { useSteward } from "../steward/context.jsx";

export function Gate({ what = "this page" }) {
  const { credential } = useSteward();
  const [value, setValue] = useState("");
  const [complaint, setComplaint] = useState(null);

  return (
    <Panel title="Unlock the write path" tone="ember" className="max-w-[560px]">
      <p className="mt-0 mb-4 text-[12px] leading-[1.7] text-dim">
        {what} reads and writes through steward, which gates every route on one shared
        secret — <code className="text-ember">STEWARD_TOKEN</code>. Paste it and it stays in
        this tab's <code className="text-ember">sessionStorage</code>, sent as a bearer header
        to this same origin. Closing the tab forgets it, and nothing is ever written into the
        build.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (!value.trim()) {
            setComplaint(
              "Paste the token, or say out loud that this steward runs open (steward serve --allow-open).",
            );
            return;
          }
          credential.remember(value);
          setValue("");
        }}
      >
        <Field label="token" hint="Compared against STEWARD_TOKEN in steward's own environment.">
          <Input
            type="password"
            autoComplete="off"
            spellCheck="false"
            placeholder="the value of STEWARD_TOKEN"
            value={value}
            invalid={Boolean(complaint)}
            onChange={(event) => {
              setValue(event.target.value);
              setComplaint(null);
            }}
          />
        </Field>
        {complaint ? <p className="mb-3 mt-0 text-[11px] text-fail">{complaint}</p> : null}
        <Actions>
          <Button tone="primary" type="submit">
            Unlock
          </Button>
          <Button onClick={() => credential.declareOpen()}>Steward runs open</Button>
        </Actions>
      </form>
      {credential.ephemeral ? (
        <p className="mb-0 mt-4 text-[10.5px] leading-[1.6] text-faint">
          This browser refuses storage, so the token lives only until this page reloads.
        </p>
      ) : null}
      <p className="mb-0 mt-4 text-[10.5px] leading-[1.6] text-faint">
        A resident's session credential will not do: steward answers{" "}
        <code>403 session_credential_forbidden</code> to every write on this page, on purpose.
      </p>
      <p className="mb-0 mt-2">
        <Note>
          Swapping this for a real operator credential — so the master token never lands in a
          browser again — is warren#225. The client behind it takes any credential with the
          same two methods.
        </Note>
      </p>
    </Panel>
  );
}

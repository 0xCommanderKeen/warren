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
import { OPERATOR_PREFIX } from "../steward/credential.js";

export function Gate({ what = "this page" }) {
  const { credential } = useSteward();
  const [value, setValue] = useState("");
  const [complaint, setComplaint] = useState(null);

  return (
    <Panel title="Unlock the write path" tone="ember" className="max-w-[560px]">
      <p className="mt-0 mb-4 text-[12px] leading-[1.7] text-dim">
        {what} reads and writes through steward, which gates every route on a bearer
        credential. Paste your <strong className="text-ink">operator credential</strong> and it
        stays in this tab's <code className="text-ember">sessionStorage</code>, sent as a bearer
        header to this same origin. Closing the tab forgets it, and nothing is ever written
        into the build.
      </p>
      <p className="mt-0 mb-4 text-[12px] leading-[1.7] text-dim">
        You mint one on a terminal, once, and steward prints it once:
        <code className="mt-1.5 block text-ember">steward operator mint &lt;your name&gt;</code>
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (!value.trim()) {
            setComplaint(
              "Paste your operator credential, or say out loud that this steward runs open (steward serve --allow-open).",
            );
            return;
          }
          credential.remember(value);
          setValue("");
        }}
      >
        <Field
          label="operator credential"
          hint="Looked up by digest against the operators steward has minted. Revoking it takes effect on the next request."
        >
          <Input
            type="password"
            autoComplete="off"
            spellCheck="false"
            placeholder={`${OPERATOR_PREFIX}…`}
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
          This browser refuses storage, so the credential lives only until this page reloads.
        </p>
      ) : null}
      <p className="mb-0 mt-4 text-[10.5px] leading-[1.6] text-faint">
        A resident's session credential will not do: steward answers{" "}
        <code>403 session_credential_forbidden</code> to every write on this page, on purpose.
      </p>
      <p className="mb-0 mt-2">
        <Note>
          The master <code>STEWARD_TOKEN</code> is still accepted, and is still the wrong thing
          to paste here: it names nobody in the audit trail, it is the same secret that boots
          the server, and revoking it means restarting. An operator credential is revocable on
          the next request and puts your name on every commit it makes.
        </Note>
      </p>
    </Panel>
  );
}

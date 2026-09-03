import { Link } from "../navigation.jsx";
import { routeTo } from "../routes.js";
import { useSteward } from "../steward/context.jsx";
import { Gate } from "../console/Gate.jsx";
import { PageHead, Section, buttonClass } from "../console/ui.jsx";
import ResidentDeclaration from "./ResidentDeclaration.jsx";
import ResidentDetail from "./ResidentDetail.jsx";
import ResidentList from "./ResidentList.jsx";
import ResidentNew from "./ResidentNew.jsx";

const HEADS = {
  residents: [
    "Residents",
    "Everything steward could validate under its residents tree. A manifest that did not " +
      "validate is named below rather than quietly left out — a fleet list that hides a broken " +
      "resident is worse than one that shows nothing.",
  ],
  residentNew: [
    "New resident",
    "This writes residents/<id>/manifest.yaml and soul.md, reads them straight back through " +
      "the ordinary validator, and commits them. Tick deploy and the same declaration goes to " +
      "the nursery, which provisions it on the host the manifest names.",
  ],
  residentDeclaration: [
    null,
    "The editable source of one resident — both files, together. Not the projection the fleet " +
      "page draws, but what is actually in git. It is a full replacement rather than a patch, " +
      "because merging a partial edit would mean steward deciding whether a missing key meant " +
      "cleared or untouched.",
  ],
};

const GATES = {
  residents: "The residents tree",
  residentNew: "Declaring a resident",
  residentDeclaration: "A resident's declaration",
  resident: "A resident's record",
};

export default function ResidentsPage({ page, params, model }) {
  const { locked } = useSteward();

  // The detail page draws its own head, because the head carries the resident's own accent
  // and its name is a thing this page has not read yet.
  const head = HEADS[page];
  const [title, standfirst] = head || [];

  return (
    <>
      {head ? <PageHead title={title ?? params.id}>{standfirst}</PageHead> : null}

      {locked ? (
        <Gate what={GATES[page] || "This page"} />
      ) : page === "residentDeclaration" ? (
        <ResidentDeclaration key={params.id} id={params.id} />
      ) : page === "residentNew" ? (
        <ResidentNew />
      ) : page === "resident" ? (
        <ResidentDetail id={params.id} model={model} />
      ) : (
        <>
          <ResidentList />
          <Section>Declaring one</Section>
          <p className="max-w-[78ch] text-[12px] leading-[1.7] text-dim">
            The nursery flow behind <code>POST /residents</code> writes both files, validates
            them, and commits — and deploys them too, if you ask it to.
          </p>
          <p className="mt-3">
            <Link to={routeTo.residentNew()} className={buttonClass("primary")}>
              New resident
            </Link>
          </p>
        </>
      )}
    </>
  );
}

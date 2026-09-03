"""Skill-library HTTP routes and their request vocabulary."""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import Field

from steward import authoring as au
from steward.input_bounds import IDENTIFIER_MAX_CHARS
from steward.manifest import validate_path
from steward.routes.deps import DOCUMENT_MAX_CHARS, Deps, _Body, _refuse
from steward.skills import SkillLibrary, effective_skills, library_for


class SkillBody(_Body):
    """One skill, as the library stores it and a form edits it.

    ``defaults`` is the field to look at twice: it is not a property of this skill so much
    as a grant to the entire fleet, since a default skill is held by every resident without
    any manifest saying so.
    """

    description: str = Field(
        min_length=1,
        max_length=DOCUMENT_MAX_CHARS,
        description="One line saying what this skill is for.",
    )
    body: str = Field(
        min_length=1, max_length=DOCUMENT_MAX_CHARS, description="The instructions themselves."
    )
    defaults: bool = Field(
        default=False, description="Give this skill to every resident, granted or not."
    )
    revision: str | None = Field(
        default=None,
        max_length=IDENTIFIER_MAX_CHARS,
        description="The revision this edit was made against. Omit it to overwrite blindly.",
    )


class SkillPost(SkillBody):
    """A skill to add to the library, which names itself."""

    name: str = Field(
        min_length=1,
        max_length=IDENTIFIER_MAX_CHARS,
        description="The skill's slug; it becomes the directory name.",
    )


def router(deps: Deps) -> APIRouter:  # noqa: C901 — route factory is assembly
    """Build the skill routes around one application collaborator graph."""
    routes = APIRouter()
    settings = deps.settings
    db = deps.db
    residents_dir = deps.residents_dir

    def skills_view(current: SkillLibrary) -> dict[str, Any]:
        """Render the library and who holds each skill."""
        result = validate_path(residents_dir, settings.skills_dir)
        holders: dict[str, list[str]] = {skill.name: [] for skill in current}
        for resident in result.residents:
            for skill in effective_skills(resident.manifest, current):
                holders[skill.name].append(resident.id)
        return {
            "library": str(current.path) if current.path is not None else None,
            "skills": [{**skill.as_dict(), "holders": holders[skill.name]} for skill in current],
            "errors": [diagnostic.render() for diagnostic in current.diagnostics],
        }

    @routes.get("/skills")
    def list_skills() -> dict[str, Any]:
        """List the skills library, and who holds each skill.

        Read from disk per request rather than from the copy this app started with. That
        was always the honest thing to serve and is now the necessary one: since a skill
        can be written over HTTP (steward #214), a listing built from a startup snapshot
        would not contain the skill the caller just created. It costs nothing extra —
        ``validate_path`` on the line below already re-reads the same library.
        """
        return skills_view(library_for(residents_dir, settings.skills_dir))

    @routes.get("/skills/{name}")
    def get_skill(name: str) -> dict[str, Any]:
        """Return one skill's frontmatter and body, with the revision to edit against."""
        root = au.resolve_skills_dir(residents_dir, settings.skills_dir)
        if root is None:
            _refuse(404, "unknown_skill", f"there is no skills library beside {residents_dir}")
        try:
            document, revision = au.read_skill_document(root, name)
        except au.AuthoringError as exc:
            deps.refuse_write(exc)
        return {
            "name": document.name,
            "description": document.description,
            "body": document.body,
            "defaults": document.default,
            "revision": revision,
            "path": str(root / name / "SKILL.md"),
        }

    def write_one_skill(
        document: au.SkillDocument,
        request: Request,
        *,
        created: bool,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        """Validate, write and commit one skill — the shared half of POST and PUT."""
        root = au.resolve_skills_dir(residents_dir, settings.skills_dir)
        if root is None:
            # No library yet. The default location beside the tree is where one belongs,
            # and it is created only once the write has actually been accepted.
            root = Path(residents_dir).resolve().parent / "skills"
        request_id = deps.accept(request, "written", {"skill": document.name})
        try:
            written = au.write_skill(
                residents_dir,
                root,
                document,
                request_id=request_id,
                principal=deps.acting_principal(request),
                created=created,
                expected_revision=expected_revision,
                **deps.write_settings(request),
            )
        except au.AuthoringError as exc:
            db.set_request_outcome(request_id, f"refused: {exc.reason}")
            deps.refuse_write(exc)
        return {
            "request_id": request_id,
            "status": "accepted",
            "name": document.name,
            "revision": written.revision,
            "paths": [str(p) for p in written.paths],
            "commit": written.commit.to_dict(),
            "warnings": [au.diagnostic_as_dict(d) for d in written.validation.warnings],
            "message": (
                f"written and validated against the fleet; {written.commit.note}. Sessions "
                f"opened from now on are provisioned with it"
            ),
        }

    @routes.post("/skills", status_code=201)
    def create_skill(body: SkillPost, request: Request) -> dict[str, Any]:
        """Add a skill to the library.

        **Human callers only.** Refuses an existing name rather than overwriting it: a
        ``POST`` that quietly replaced somebody's skill would make "add" and "rewrite" the
        same button.

        ``defaults: true`` deserves a second look before sending. A default skill is held
        by every resident in the fleet without any manifest granting it, so this one flag
        changes what every session is given.
        """
        return write_one_skill(
            au.SkillDocument(
                name=body.name,
                description=body.description,
                body=body.body,
                default=body.defaults,
            ),
            request,
            created=True,
        )

    @routes.put("/skills/{name}")
    def update_skill(name: str, body: SkillBody, request: Request) -> dict[str, Any]:
        """Replace one skill in the library, if it still validates for the whole fleet.

        **Human callers only**, like every write here.
        """
        return write_one_skill(
            au.SkillDocument(
                name=name, description=body.description, body=body.body, default=body.defaults
            ),
            request,
            created=False,
            expected_revision=body.revision,
        )

    return routes

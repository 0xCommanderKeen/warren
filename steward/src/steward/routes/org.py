"""The org chart route: one read, no state (warren#441)."""

from typing import Any

from fastapi import APIRouter

from steward.manifest import validate_path
from steward.org import org_chart
from steward.routes.deps import Deps


def router(deps: Deps) -> APIRouter:
    """Build the org route around one application collaborator graph."""
    routes = APIRouter()
    settings = deps.settings
    residents_dir = deps.residents_dir

    @routes.get("/org")
    def get_org() -> dict[str, Any]:
        """Return the fleet as nodes and delegation edges, computed from the manifests.

        A projection of :func:`~steward.manifest.validate_path` and nothing else — no
        ledger, no host, no clock — so two calls a second apart over an unchanged tree
        answer the same bytes, and a resident that gained a grant this morning is on the
        chart without anybody having redrawn it.

        A manifest steward could not read is not a node — it never became a
        :class:`~steward.manifest.Resident` — so the chart is drawn from the residents
        validation produced and the refusals are reported beside it in ``errors``, the way
        ``GET /residents`` reports them. A fleet that has gone quiet says why rather than
        answering an empty chart. (``errors`` also carries the diagnostics validation
        raises *about* a tree whose residents did load, such as two residents claiming one
        home; those residents are on the chart, and the complaint is right there with it.)
        """
        result = validate_path(residents_dir, settings.skills_dir)
        return {
            **org_chart(result.residents).to_dict(),
            "errors": [diagnostic.render() for diagnostic in result.errors],
        }

    return routes

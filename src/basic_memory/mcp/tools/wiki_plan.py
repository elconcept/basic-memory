"""Portable stage plans without host hooks, subprocesses or local JSON caches."""

from typing import Literal
from uuid import uuid4

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import JsonValue

from basic_memory.mcp.clients.knowledge import KnowledgeClient
from basic_memory.mcp.project_context import get_project_client
from basic_memory.mcp.server import mcp
from basic_memory.wiki_ingest.models import Plan, Step
from basic_memory.wiki_ingest.service import event
from basic_memory.wiki_ingest.store import ApiNoteStore


@mcp.tool(
    title="Wiki workflow plan",
    description="Create, resume and update durable ingest plans in Basic Memory. "
    "Completion requires every step to be done or explicitly skipped with evidence. "
    "Use search_notes(note_types=['wiki_plan']) to discover plans across sessions.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
)
async def wiki_plan(
    action: Literal["create", "read", "step", "pause", "resume", "abandon", "finish"],
    project: str,
    identifier: str | None = None,
    title: str = "",
    content: str = "",
    steps: list[str] | None = None,
    step: int | None = None,
    state: Literal["pending", "active", "done", "failed", "skipped"] = "active",
    evidence: str = "",
    project_id: str | None = None,
    context: Context | None = None,
) -> dict[str, JsonValue]:
    """step is a 1-based stage number; evidence records the result or reason."""
    async with get_project_client(project, context=context, project_id=project_id) as (
        client,
        active,
    ):
        store = ApiNoteStore(KnowledgeClient(client, active.external_id))
        old = None
        if action == "create":
            if not title.strip() or not steps or any(not s.strip() for s in steps):
                raise ToolError("Nonempty title and steps are required")
            plan = Plan(title=title, content=content, steps=[Step(title=s) for s in steps])
            name = "plan-" + uuid4().hex
        else:
            if identifier is None:
                raise ToolError("Plan identifier is required")
            old = await store.read(identifier)
            if old is None or "wiki_plan" not in old.metadata:
                raise ToolError("Plan not found; legacy plans remain readable with read_note")
            plan = Plan.model_validate(old.metadata["wiki_plan"])
            name = str(old.metadata["plan_name"])
            if action == "read":
                return {"identifier": old.identifier, "plan": plan.model_dump(mode="json")}
            if action == "step":
                if plan.state != "active":
                    raise ToolError("Resume the plan before updating a step")
                if step is None or not 1 <= step <= len(plan.steps):
                    raise ToolError("Step outside plan")
                if state in {"done", "failed", "skipped"} and not evidence.strip():
                    raise ToolError("Step result/reason is required")
                plan.steps[step - 1].state = state
                plan.steps[step - 1].evidence = evidence
            elif action in {"pause", "abandon"}:
                if not evidence.strip():
                    raise ToolError("Reason required")
                plan.state = "paused" if action == "pause" else "abandoned"
            elif action == "resume":
                plan.state = "active"
            elif action == "finish":
                if any(s.state not in {"done", "skipped"} for s in plan.steps):
                    raise ToolError("Unfinished stages remain")
                plan.state = "done"
        plan.journal.append(event(f"{action} {step or ''}: {evidence}"))
        body = f"# {plan.title}\n\n{plan.content}\n\n## Stages\n\n"
        body += "\n".join(
            f"- [{s.state}] E{i}: {s.title} — {s.evidence}" for i, s in enumerate(plan.steps, 1)
        )
        body += "\n\n## Journal\n\n" + "\n".join(plan.journal)
        note = await store.write(
            name,
            "plany",
            body,
            {"type": "wiki_plan", "plan_name": name, "wiki_plan": plan.model_dump(mode="json")},
            old,
        )
        return {"identifier": note.identifier, "plan": plan.model_dump(mode="json")}

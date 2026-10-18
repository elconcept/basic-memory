"""Exercise the browser-facing MCP tools through the real API and SQLite store."""

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError


@pytest.mark.asyncio
async def test_ingest_and_plan_over_mcp(mcp_server, app, test_project, tmp_path, monkeypatch):
    sources = tmp_path / "documents"
    sources.mkdir()
    (sources / "source.md").write_text("# Page 1\n\nDokładna treść źródła.  \n")
    monkeypatch.setenv(
        "BASIC_MEMORY_INGEST_SOURCES",
        json.dumps(
            {
                str(test_project.external_id): {
                    "documents": str(sources),
                }
            }
        ),
    )
    async with Client(mcp_server) as client:

        async def ingest(action, **kwargs):
            result = await client.call_tool(
                "wiki_ingest",
                {
                    "project": test_project.name,
                    "action": action,
                    **kwargs,
                },
            )
            return result.data

        await ingest("refresh")
        started = await ingest("start", path="source.md")
        assert started["state"] == "active"
        read = await ingest("read")
        assert read["text"] == (sources / "source.md").read_text()
        assert read["complete"]
        await ingest("finish", reason="No additional findings in this fixture")
        assert (await ingest("status"))["counts"]["done"] == 1
        await ingest("index")
        plan = await client.call_tool(
            "wiki_plan",
            {
                "action": "create",
                "project": test_project.name,
                "title": "Ingest fixture",
                "steps": ["Capture source"],
            },
        )
        identifier = plan.data["identifier"]
        with pytest.raises(ToolError, match="Unfinished"):
            await client.call_tool(
                "wiki_plan",
                {
                    "action": "finish",
                    "project": test_project.name,
                    "identifier": identifier,
                },
            )
        await client.call_tool(
            "wiki_plan",
            {
                "action": "step",
                "project": test_project.name,
                "identifier": identifier,
                "step": 1,
                "state": "done",
                "evidence": started["note"],
            },
        )
        result = await client.call_tool(
            "wiki_plan",
            {
                "action": "finish",
                "project": test_project.name,
                "identifier": identifier,
            },
        )
        assert result.data["plan"]["state"] == "done"


@pytest.mark.asyncio
async def test_ingest_mount_is_bound_to_authorized_project_uuid(
    mcp_server, app, test_project, monkeypatch
):
    monkeypatch.setenv(
        "BASIC_MEMORY_INGEST_SOURCES",
        json.dumps(
            {
                test_project.name: {
                    "documents": "/not-allowed-by-name",
                }
            }
        ),
    )
    async with Client(mcp_server) as client:
        with pytest.raises(ToolError, match="project UUID"):
            await client.call_tool(
                "wiki_ingest", {"project": test_project.name, "action": "status"}
            )

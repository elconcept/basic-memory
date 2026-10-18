---
title: wiki-plan(3)
type: manpage
section: 3
name: wiki-plan
summary: maintain durable stage plans across MCP clients
generated: registry
tool: wiki_plan
---

# wiki-plan(3)

## NAME

**wiki-plan** — maintain durable stage plans across MCP clients

## SYNOPSIS

```
wiki_plan(action, project, identifier=None, title="", content="",
          steps=None, step=None, state="active", evidence="",
          project_id=None)
```

## PARAMETERS

- **action** (string, required)
- **project** (string, required)
- **identifier** (string | null, optional, default: None)
- **title** (string, optional, default: "")
- **content** (string, optional, default: "")
- **steps** (array | null, optional, default: None)
- **step** (integer | null, optional, default: None)
- **state** (string, optional, default: "active")
- **evidence** (string, optional, default: "")
- **project_id** (string | null, optional, default: None)

## DESCRIPTION

Create a plan with `action="create"`, a title, full plan content and an ordered
list of stage titles. The returned identifier is portable across sessions and
clients. Find plans with `search_notes(note_types=["wiki_plan"])`.

`read` returns current state. `step` updates a 1-based stage number; `done`,
`failed` and `skipped` require evidence or a reason. `pause` and `abandon` require
a reason; `resume` reopens work. `finish` refuses while any stage is pending,
active or failed. Every mutation is checksum-guarded and verified by reading it.

The invoking model performs the work. No local OpenCode runner, fixed model name,
client-side cache or Claude hook is required. Existing `type: plan` notes remain
accessible through ordinary note tools; historical local JSON plans can be
preserved with `wiki_ingest(action="migrate", ...)`.

## SEE ALSO

- see_also [[wiki-ingest(3)]]
- see_also [[search-notes(3)]]

# SSB Housing Dashboard
**Google ADK + MCP + SSB PxWebAPI v2 · Multi-Agent Prototype**

```
┌──────────────────────────────────────────────────────────────────┐
│  adk web  (http://localhost:8000)                                │
│   └── OrchestratorAgent (root_agent)                            │
│         ├── AgentTool → price_index_agent                        │
│         │     └── McpToolset → get_house_price_index             │
│         │                   → list_available_periods("07230")    │
│         ├── AgentTool → regional_agent                           │
│         │     └── McpToolset → get_sqm_price_by_region           │
│         │                   → list_available_periods("06035")    │
│         └── AgentTool → search_agent                             │
│               └── McpToolset → search_ssb_tables                 │
│                                                                  │
│  before_tool_callback: guardrail() on every agent               │
│                              │ streamable-http                   │
│                              ▼                                   │
│         MCP Server  (http://localhost:8002/mcp)                  │
│         Web UI      (http://localhost:8003)                      │
│                              │ HTTPS                             │
│                              ▼                                   │
│         SSB PxWebAPI v2  (data.ssb.no)                           │
└──────────────────────────────────────────────────────────────────┘
```

## Prerequisites

- Python 3.12+
- A **Google AI Studio API key** (free): https://aistudio.google.com/apikey

## Setup

```bash
# 1. Create the virtual environment (Python 3.12, isolated from system packages)
python3 -m venv .venv

# 2. Activate it
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

# 3. Install all pinned dependencies into the venv
pip install -r requirements.txt
```

> The `.venv/` folder is self-contained — no conflicts with other system packages.
> All dependencies are pinned to exact versions in `requirements.txt`.

## API key

Put your Google API key in `adk_agent/.env` — `adk web` reads it automatically:

```
# adk_agent/.env
GOOGLE_API_KEY=your-key-here
```

You can also export it in the terminal instead:
```bash
export GOOGLE_API_KEY=your-key-here
```

## Running

Make sure the venv is **activated** (`source .venv/bin/activate`) in each terminal.

### Terminal 1 — MCP Server
```bash
python mcp_server/server.py
# MCP endpoint → http://localhost:8002/mcp
# Web UI       → http://localhost:8003
```

### Terminal 2 — ADK Web
```bash
cd adk_agent
adk web
# Agent UI → http://localhost:8000
```

## URLs at a glance

| URL | What it is |
|-----|-----------|
| `http://localhost:8000` | ADK Web — chat with the agent here |
| `http://localhost:8002/mcp` | MCP endpoint — used by agents internally, not for browsers |
| `http://localhost:8003` | MCP Server monitor — live logs, tool calls, stats |

## Project structure

```
ssb_housing/
├── .venv/                          ← isolated Python 3.12 environment (git-ignored)
├── requirements.txt                ← all packages pinned with exact versions
├── mcp_server/
│   └── server.py                   ← FastMCP server + embedded web UI monitor
├── adk_agent/
│   ├── .env                        ← GOOGLE_API_KEY goes here
│   └── ssb_housing_agent/
│       ├── __init__.py             ← exposes root_agent (required by adk web)
│       └── agent.py                ← all agents + guardrail defined here
└── README.md
```

## Multi-agent architecture

The system uses Google ADK's `AgentTool` pattern — an orchestrator `LlmAgent`
that delegates to specialist sub-agents based on the user's question.

### Agents

| Agent | Role |
|-------|------|
| `ssb_housing_orchestrator` | Root agent. Reads user intent, decides which specialists to call, synthesises a final answer |
| `price_index_agent` | Fetches yearly house price index (table 07230). Validates time periods before fetching |
| `regional_agent` | Fetches NOK/m² prices by county (table 06035). Validates time periods before fetching |
| `search_agent` | Searches SSB's full table catalogue for topics beyond the default tables |

### How agents communicate

```
User question
    ↓
OrchestratorAgent thinks → calls AgentTool(price_index_agent)
    ↓
price_index_agent runs fully (calls MCP tools, reasons, writes response)
    ↓
price_index_agent's final text response is returned to the orchestrator
as a tool result — just like a normal function returning a value
    ↓
Orchestrator may call more AgentTools, then synthesises everything
into one final answer for the user
```

### Guardrail

Every tool call across all agents passes through `guardrail()` via
`before_tool_callback`. It runs before the MCP server is ever contacted.

Rules enforced:
- Only tools on the `ALLOWED_TOOLS` allowlist can be called
- `max_results` is capped at 200
- Wildcard periods (`*`) are blocked on the regional table (too many rows)
- Search queries must be at least 2 characters

If blocked, the guardrail returns a structured error the orchestrator reads
and explains to the user, suggesting a valid alternative.

## MCP Tools

| Tool | Used by | Description |
|------|---------|-------------|
| `get_house_price_index(periods)` | `price_index_agent` | Yearly price index by dwelling type (table 07230) |
| `get_sqm_price_by_region(periods)` | `regional_agent` | NOK/m² by county (table 06035) |
| `list_available_periods(table_id)` | `price_index_agent`, `regional_agent` | Validates what time periods exist before fetching |
| `search_ssb_tables(query)` | `search_agent` | Searches SSB's full catalogue by keyword |

## MCP Server monitor (port 8003)

Open `http://localhost:8003` while the system is running to see:
- **Live log stream** — every log line in real time, colour coded by level
- **Tool call table** — each invocation with tool name, params, rows returned, latency, status
- **KPI cards** — total tool calls, SSB requests, rows returned, errors

## SSB Tables used

| Table | Description |
|-------|-------------|
| `07230` | Price index for existing dwellings (2015=100), yearly |
| `06035` | Average square metre prices by region & dwelling type, yearly |

Both are open, no registration required (CC BY 4.0).

## Example queries

- *"Show me the house price index trend for the last 10 years"*
- *"Compare square metre prices across Norwegian regions"*
- *"What's the year-over-year change for detached houses vs apartments?"*
- *"Show me prices in Oslo vs Bergen for the last 5 years"*
- *"What SSB tables are available for rental prices?"*
- *"What years of data are available for the regional price table?"*



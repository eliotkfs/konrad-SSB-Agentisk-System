# SSB Housing Dashboard
**Google ADK + MCP + SSB PxWebAPI v2 · Multi-Agent Prototype**

---

## System Description

This is an AI-powered analysis system for the Norwegian housing market, built
on Statistics Norway's (SSB) open data. The user asks questions in natural
language — e.g. *"What has happened to house prices over the last 10 years?"*
— and the system automatically fetches, analyses, and presents the relevant data.

The system is made up of three layers:

**1. MCP Server** — a FastMCP-based API that wraps SSB PxWebAPI v2 and exposes
four tools the agents can call. Also runs a live monitoring dashboard in the
browser so you can watch every tool call in real time.

**2. Multi-Agent System** — four Google ADK agents that collaborate: an
orchestrator agent that understands the user's intent and delegates to three
specialist agents (price index, regional analysis, table search). A guardrail
validates every tool call before it executes.

**3. ADK Web** — Google's built-in chat UI for interacting with the agent
system, with a full trace view of all agent and tool calls.

---

## System Diagrams

### Overall Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  adk web  (http://localhost:8000)                                │
│                                                                  │
│   OrchestratorAgent  ←── understands user question              │
│         │                delegates to right specialist           │
│         │                synthesises final answer                │
│         │                                                        │
│         ├── AgentTool → price_index_agent                        │
│         │     └── get_house_price_index                          │
│         │         list_available_periods("07230")                │
│         │                                                        │
│         ├── AgentTool → regional_agent                           │
│         │     └── get_sqm_price_by_region                        │
│         │         list_available_periods("06035")                │
│         │                                                        │
│         └── AgentTool → search_agent                             │
│               └── search_ssb_tables                              │
│                                                                  │
│   before_tool_callback: guardrail() — runs on all agents         │
│                              │                                   │
│                              │ streamable-http                   │
│                              ▼                                   │
│         MCP Server  (http://localhost:8002/mcp)                  │
│         Monitor UI  (http://localhost:8003)                      │
│                              │                                   │
│                              │ HTTPS                             │
│                              ▼                                   │
│         SSB PxWebAPI v2  (data.ssb.no)                           │
└──────────────────────────────────────────────────────────────────┘
```

### Data Flow for a User Question

```
User types question in adk web
          ↓
OrchestratorAgent analyses intent
          ↓
    ┌─────┴──────┐
    ▼            ▼
price_index_  regional_
agent         agent
    │            │
    ▼            ▼
guardrail()  guardrail()   ← validates args before any MCP call
    │            │
    ▼            ▼
MCP Server   MCP Server    ← calls SSB API
    │            │
    ▼            ▼
SSB 07230    SSB 06035
    │            │
    └─────┬──────┘
          ▼
  Each agent's answer returned to orchestrator as plain text
          ↓
  Orchestrator synthesises one combined response
          ↓
  User sees finished analysis in adk web
```

### How Agents Return Answers to the Orchestrator

```
Orchestrator calls AgentTool(price_index_agent)
    ↓
price_index_agent runs fully:
    - calls list_available_periods (if needed)
    - calls get_house_price_index
    - reasons over the data
    - writes a text response
    ↓
That text response is handed back to the orchestrator
as a tool result — identical to how a normal function returns a value
    ↓
Orchestrator sees only the finished answer, not the internal steps
```

---

## Why This Agent System and MCP Server?

### Why Google ADK with AgentTool?

ADK's `AgentTool` pattern lets the orchestrator **decide at runtime** which
specialists to call based on the question — rather than always running a fixed
pipeline. This means:

- A question about price trends only triggers `price_index_agent`
- A question about regional prices only triggers `regional_agent`
- A broad question like "give me a full market overview" triggers both in sequence
- An unknown topic triggers `search_agent` to discover the right SSB table first

`SequentialAgent` and `ParallelAgent` were considered but rejected because they
run sub-agents in a fixed order regardless of the question — not appropriate
when routing depends on user intent.

### Why MCP (Model Context Protocol)?

MCP creates a clean separation between **tools** (the MCP server) and
**intelligence** (the agents). Benefits:

- The MCP server can be replaced or extended without touching agent code
- Any MCP-compatible client can connect to it, not just this ADK system
- Tool logic (HTTP calls, parsing, retries) stays out of the agent layer
- FastMCP's `streamable-http` transport is stable and well-supported

### Why SSB PxWebAPI v2?

Statistics Norway's API is open, free, requires no authentication, and is
licenced CC BY 4.0. PxWebAPI v2 is the current standard for Norwegian public
statistics and returns structured JSON-stat2 format which is straightforward
to parse.

---

## How to Use the System

Once running (see setup below), open **http://localhost:8000** in your browser.
You will see the ADK Web chat interface. Type a question in plain English and
press Enter.

The orchestrator will automatically:
1. Route your question to the right specialist agent(s)
2. Call the relevant SSB data tools via the MCP server
3. Return a synthesised answer with concrete numbers and trends

You can also open **http://localhost:8003** in a second tab to watch the MCP
server monitor — every tool call, log line, and SSB API request appears live.

**What you can ask:**
- Questions about price trends over time
- Regional comparisons between Norwegian counties
- Specific dwelling type comparisons (detached vs apartments)
- Requests for a specific year range
- Questions about SSB data topics beyond the default tables (search agent will find them)

**What the guardrail blocks:**
- Unknown tools not on the allowed list
- Requests for more than 50 results at once
- Wildcard period queries (`*`) on the large regional table
- Empty or single-character search queries

---

## Example: User Query with Result

**User asks:**
> "Compare house prices in Oslo vs the national average for the last 5 years,
> and tell me which dwelling type has grown the most."

**What happens internally:**
```
Orchestrator → calls price_index_agent ("last 5 years, by dwelling type")
               calls regional_agent ("Oslo vs national, last 5 years")

price_index_agent:
  → guardrail: ✓ allowed
  → list_available_periods("07230") — confirms 2020–2024 exists
  → get_house_price_index(periods="top(5)")
  ← returns index data for all dwelling types, 2020–2024

regional_agent:
  → guardrail: ✓ allowed
  → list_available_periods("06035") — confirms range
  → get_sqm_price_by_region(periods="top(5)")
  ← returns NOK/m² for all regions including Oslo, 2020–2024

Orchestrator synthesises both results → final answer
```

**Example output:**
> Over the last 5 years (2020–2024), Norwegian house prices rose significantly
> across all dwelling types. Detached houses saw the strongest growth, with the
> index reaching 194.1 in 2024 (2015=100), up 5.1% from 2023 and 21.3% over
> the full 5-year period. Apartments grew more modestly at +14.8% over the
> same period (index: 183.7).
>
> In Oslo, the average square metre price reached NOK 87 400/m² in 2024,
> compared to a national average of NOK 51 200/m² — making Oslo 70% more
> expensive than the national average. Bergen was second at NOK 58 900/m².

---

## Developer Setup — Download, Configure, and Run

### Prerequisites

- Python 3.12+
- A **Google AI Studio API key** (free): https://aistudio.google.com/apikey

### 1. Get the code

```bash
git clone https://github.com/eliotkfs/konrad-SSB-Agentisk-System.git
cd ssb_housing
```

Or simply copy the project folder to your machine.

### 2. Create the virtual environment

```bash
python3 -m venv .venv
```

This creates an isolated Python 3.12 environment inside the project folder,
completely separate from any other packages on your system.

### 3. Activate the virtual environment

```bash
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows CMD
# .venv\Scripts\Activate.ps1     # Windows PowerShell
```

You will see `(.venv)` appear in your terminal prompt. You need to do this
in every new terminal window you open for this project.

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

All packages are pinned to exact versions — no version conflicts.

### 5. Set your Google API key

Create `adk_agent/.env` and paste the following:

```
GOOGLE_API_KEY=your-actual-key-here
```

`adk web` reads this file automatically. Alternatively, export it in the terminal:

```bash
export GOOGLE_API_KEY=your-actual-key-here
```

Verify it was set correctly:
```bash
echo $GOOGLE_API_KEY
```

### 6. Run the system

Open **two terminal windows**, activate the venv in each (`source .venv/bin/activate`).

**Terminal 1 — MCP Server:**
```bash
python mcp_server/server.py
```
You should see:
```
MCP endpoint → http://localhost:8002/mcp
Web UI       → http://localhost:8003
```

**Terminal 2 — ADK Web:**
```bash
cd adk_agent
adk web
```
You should see:
```
ADK Web Server started
For local testing, access at http://127.0.0.1:8000
```

### 7. Open in browser

| URL | Purpose |
|-----|---------|
| `http://localhost:8000` | ADK Web — chat with the agent here |
| `http://localhost:8003` | MCP monitor — live logs and tool call stats |
| `http://localhost:8002/mcp` | MCP endpoint — used by agents internally, not for browsers |

---

## Project Structure

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

## MCP Tools

| Tool | Used by | Description |
|------|---------|-------------|
| `get_house_price_index(periods)` | `price_index_agent` | Yearly price index by dwelling type (table 07230) |
| `get_sqm_price_by_region(periods)` | `regional_agent` | NOK/m² by county (table 06035) |
| `list_available_periods(table_id)` | `price_index_agent`, `regional_agent` | Validates what time periods exist before fetching |
| `search_ssb_tables(query)` | `search_agent` | Searches SSB's full catalogue by keyword |

## SSB Tables

| Table | Description | Frequency |
|-------|-------------|-----------|
| `07230` | Price index for existing dwellings (2015=100) | Yearly |
| `06035` | Average square metre prices by region & dwelling type | Yearly |

Both are open, no registration required (CC BY 4.0).


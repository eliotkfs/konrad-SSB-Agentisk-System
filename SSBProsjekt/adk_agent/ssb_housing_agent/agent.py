"""
SSB Housing — Multi-Agent System with Guardrail
================================================
Architecture:

   User message
        ↓
   guardrail()  ← evaluates every tool call before it executes
        ↓ allow / block
                    ┌─────────────────────────────┐
                    │   OrchestratorAgent (root)   │
                    └────────────┬────────────────┘
                                 │ AgentTool
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                   ▼
   PriceIndexAgent        RegionalAgent        SearchAgent

Run with:
    cd adk_agent && adk web
"""

import logging
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools import BaseTool
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StreamableHTTPConnectionParams
from google.adk.tools.tool_context import ToolContext

log = logging.getLogger("ssb-guardrail")

# ── Config ─────────────────────────────────────────────────────────────────────

MCP_URL = "http://localhost:8002/mcp"
MODEL   = "gemini-2.5-flash"

def _mcp() -> McpToolset:
    return McpToolset(connection_params=StreamableHTTPConnectionParams(url=MCP_URL))

# ── Allowed tools & rules ──────────────────────────────────────────────────────

ALLOWED_TOOLS = {
    "get_house_price_index",
    "get_sqm_price_by_region",
    "list_available_periods",
    "search_ssb_tables",
    # AgentTools (sub-agents called by orchestrator)
    "price_index_agent",
    "regional_agent",
    "search_agent",
}

# Hard limits to prevent accidental huge requests
MAX_RESULTS_LIMIT = 200


def _validate_args(tool_name: str, args: dict) -> tuple[bool, str]:
    """
    Rule-based validation of tool arguments.
    Returns (allowed: bool, reason: str).
    """
    # Block unknown tools entirely
    if tool_name not in ALLOWED_TOOLS:
        return False, f"Tool '{tool_name}' is not on the allowed list."

    # Prevent absurdly large result requests
    if "max_results" in args:
        try:
            if int(args["max_results"]) > MAX_RESULTS_LIMIT:
                return False, (
                    f"max_results={args['max_results']} exceeds the limit of "
                    f"{MAX_RESULTS_LIMIT}. Please request fewer results."
                )
        except (ValueError, TypeError):
            pass

    # Block wildcard periods on large tables (would return thousands of rows)
    if tool_name == "get_sqm_price_by_region":
        periods = args.get("periods", "")
        if periods == "*":
            return False, (
                "Fetching all periods ('*') for the regional table returns too many rows. "
                "Please use 'top(N)' or a specific year range like '2020-2024'."
            )

    # Validate search queries aren't empty or nonsensical
    if tool_name == "search_ssb_tables":
        query = args.get("query", "").strip()
        if len(query) < 2:
            return False, "Search query is too short. Please provide a meaningful keyword."

    return True, "ok"


# ── Guardrail callback ─────────────────────────────────────────────────────────

async def guardrail(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
) -> dict | None:
    """
    Runs before every tool call across all agents in the system.

    - Returns None  → allow the call through unchanged
    - Returns dict  → block the call, return this dict as the result instead
    """
    tool_name = tool.name
    user_id   = tool_context.user_id or "unknown"

    log.info(f"GUARDRAIL checking  tool={tool_name}  args={args}  user={user_id}")

    allowed, reason = _validate_args(tool_name, args)

    if allowed:
        log.info(f"GUARDRAIL ✓ allowed  tool={tool_name}")
        return None   # let it through

    # Blocked — log and return a structured error the agent can read and explain
    log.warning(f"GUARDRAIL ✗ blocked  tool={tool_name}  reason={reason}")
    return {
        "status":  "blocked",
        "tool":    tool_name,
        "reason":  reason,
        "message": (
            f"The request to '{tool_name}' was blocked by the guardrail. "
            f"Reason: {reason}"
        ),
    }


# ── Sub-agents ─────────────────────────────────────────────────────────────────

price_index_agent = LlmAgent(
    model=MODEL,
    name="price_index_agent",
    description=(
        "Specialist in Norwegian house price index trends over time. "
        "Call this agent when the user wants to see how prices have changed "
        "across years, compare dwelling types (houses vs apartments), or "
        "understand long-term price movements."
    ),
    instruction="""
You are a Norwegian housing price index specialist.
You have two tools:
- list_available_periods(table_id="07230") → call this first if the user asks
  about a specific year or range, to confirm that period exists in the data
- get_house_price_index(periods) → fetches the actual yearly index data

Always:
- Fetch at least top(10) years unless asked for a specific range
- If the user requests a specific year range, call list_available_periods first
  to validate it, then fetch with the confirmed range
- Break down by dwelling type (detached, terraced, apartments, all)
- Highlight the year-over-year change for the most recent year
- Mention the base year (2015=100) when reporting index values
- Format numbers clearly: e.g. "Index 194.1 (+5.1% vs prior year)"

Return a concise, factual summary with the key numbers.
""".strip(),
    tools=[_mcp()],
    before_tool_callback=guardrail,
)

regional_agent = LlmAgent(
    model=MODEL,
    name="regional_agent",
    description=(
        "Specialist in regional Norwegian housing prices (NOK per m²). "
        "Call this agent when the user asks about prices in specific cities or "
        "counties, wants to compare regions, or asks about square metre prices."
    ),
    instruction="""
You are a Norwegian regional housing market specialist.
You have two tools:
- list_available_periods(table_id="06035") → call this first if the user asks
  about a specific year or range, to confirm it exists in the data
- get_sqm_price_by_region(periods) → fetches NOK/m² data by county

Always:
- Fetch top(8) periods unless a specific range is requested
- If the user requests a specific range, call list_available_periods("06035")
  first to validate it, then fetch with the confirmed range
- Highlight the most and least expensive regions
- Compare Oslo to the national average where relevant
- Format prices clearly: e.g. "Oslo: NOK 87 400/m² (+3.8% YoY)"
- Note if certain regions have missing data

Return a concise regional breakdown with standout comparisons.
""".strip(),
    tools=[_mcp()],
    before_tool_callback=guardrail,
)

search_agent = LlmAgent(
    model=MODEL,
    name="search_agent",
    description=(
        "Specialist in discovering SSB data tables. "
        "Call this agent when the user asks about data topics not covered by "
        "the standard housing tables — e.g. rental prices, construction starts, "
        "population, or any other statistics from SSB."
    ),
    instruction="""
You are an SSB data catalogue specialist.
Use search_ssb_tables to find relevant Statistics Norway tables, and
list_available_periods to check what time range a table covers.

When searching:
- Try both Norwegian (e.g. 'boligpris', 'husleie') and English keywords
- Return table IDs, titles, and date ranges so the user can act on them
- Suggest which table is most relevant and why

Be specific about what each found table contains.
""".strip(),
    tools=[_mcp()],
    before_tool_callback=guardrail,
)

# ── Orchestrator ───────────────────────────────────────────────────────────────

root_agent = LlmAgent(
    model=MODEL,
    name="ssb_housing_orchestrator",
    description="Orchestrator for Norwegian housing market analysis using SSB data.",
    instruction="""
You are the lead analyst for the Norwegian housing market dashboard.
You coordinate a team of specialist agents to answer user questions about
Statistics Norway (SSB) housing data.

Your specialists:
- price_index_agent  → house price index trends over time, by dwelling type;
                       also validates available time periods for table 07230
- regional_agent     → square metre prices by Norwegian county/region;
                       also validates available time periods for table 06035
- search_agent       → discovering SSB tables on any topic

How to work:
1. Read the user's question carefully.
2. Decide which specialist(s) to call — often one is enough, but for broad
   questions (e.g. "give me a full market overview") call both price_index_agent
   AND regional_agent.
3. Call the relevant agent(s) using their tools.
4. Synthesise the results into a single, clear, well-structured answer.
5. Always include concrete numbers and trends in your final answer.
6. If the user asks about something outside housing prices (e.g. rents, new builds),
   call search_agent first to find the right SSB table.
7. If a tool call was blocked by the guardrail, explain the limitation clearly
   to the user and suggest a valid alternative request.

Speak directly to the user — don't say "the specialist reported…",
just present the findings as your own coherent analysis.
""".strip(),
    tools=[
        AgentTool(agent=price_index_agent),
        AgentTool(agent=regional_agent),
        AgentTool(agent=search_agent),
    ],
    before_tool_callback=guardrail,
)
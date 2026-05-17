"""
SSB Housing MCP Server
======================
FastMCP server (port 8002) + live Web UI dashboard (port 8003).

Open http://localhost:8003 to see real-time tool calls, logs, and stats.
The ADK agent connects to http://localhost:8002/mcp
"""

import asyncio
import json
import logging
import sys
import time
from collections import deque
from datetime import datetime

import httpx
from fastmcp import FastMCP

# ── In-memory event store (shared between MCP + Web UI) ───────────────────────

class EventStore:
    """Holds recent log lines and tool call records for the web UI."""
    def __init__(self, maxlen: int = 200):
        self.logs: deque[dict] = deque(maxlen=maxlen)
        self.tool_calls: deque[dict] = deque(maxlen=50)
        self.stats = {
            "started_at": datetime.now().isoformat(),
            "total_tool_calls": 0,
            "total_ssb_requests": 0,
            "total_rows_returned": 0,
            "errors": 0,
        }
        self._subscribers: list[asyncio.Queue] = []

    def add_log(self, level: str, msg: str):
        entry = {"ts": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                 "level": level, "msg": msg}
        self.logs.append(entry)
        self._broadcast({"type": "log", **entry})

    def add_tool_call(self, record: dict):
        self.tool_calls.appendleft(record)
        self.stats["total_tool_calls"] += 1
        self._broadcast({"type": "tool_call", **record})
        self._broadcast({"type": "stats", **self.stats})

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.remove(q)

    def _broadcast(self, event: dict):
        for q in self._subscribers:
            q.put_nowait(event)

store = EventStore()

# ── Logging — terminal colours + capture to EventStore ────────────────────────

class ColouredFormatter(logging.Formatter):
    RESET = "\033[0m"; BOLD = "\033[1m"; GREY = "\033[90m"
    CYAN = "\033[96m"; YELLOW = "\033[93m"; RED = "\033[91m"; MAGENTA = "\033[95m"
    COLOURS = {logging.DEBUG: "\033[90m", logging.INFO: "\033[96m",
               logging.WARNING: "\033[93m", logging.ERROR: "\033[91m",
               logging.CRITICAL: "\033[95m"}

    def format(self, record):
        c  = self.COLOURS.get(record.levelno, self.RESET)
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        msg = record.getMessage()
        for tok in ("OK", "rows", "ms", "TOOL", "DONE"):
            msg = msg.replace(tok, f"{self.BOLD}{tok}{self.RESET}")
        return (f"{self.GREY}{ts}{self.RESET} "
                f"{c}{record.levelname:<8}{self.RESET} "
                f"{self.GREY}[ssb-mcp]{self.RESET} {msg}")


class StoreHandler(logging.Handler):
    """Forwards log records into the EventStore for the web UI."""
    def emit(self, record):
        store.add_log(record.levelname, record.getMessage())


def _setup_logging():
    term = logging.StreamHandler(sys.stdout)
    term.setFormatter(ColouredFormatter())
    web  = StoreHandler()
    logging.basicConfig(level=logging.INFO, handlers=[term, web])
    for lib in ("httpx", "httpcore", "asyncio", "fastmcp", "uvicorn"):
        logging.getLogger(lib).setLevel(logging.WARNING)
    return logging.getLogger("ssb-mcp")

log = _setup_logging()

# ── Config ─────────────────────────────────────────────────────────────────────

SSB_BASE    = "https://data.ssb.no/api/pxwebapi/v2/tables"
MCP_PORT    = 8002
UI_PORT     = 8003
MAX_RETRIES = 3
RETRY_DELAY = 1.5

# ── FastMCP ────────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="ssb-housing",
    instructions=(
        "Connected to Statistics Norway (SSB) housing data. "
        "Use tools to fetch house price indices, sqm prices, and search tables."
    ),
)

# ── HTTP helpers ───────────────────────────────────────────────────────────────

async def _get(table_id: str, params: dict, *, label: str = "") -> dict:
    url  = f"{SSB_BASE}/{table_id}/data"
    tag  = label or table_id
    log.info(f"→ GET  table={tag}")
    store.stats["total_ssb_requests"] += 1

    for attempt in range(1, MAX_RETRIES + 1):
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                r = await client.get(url, params={"lang": "en", **params})
            ms = (time.perf_counter() - t0) * 1000
            kb = len(r.content) / 1024
            log.info(f"← {r.status_code}  {ms:.0f}ms  {kb:.1f}KB  table={tag}")
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            log.error(f"HTTP {e.response.status_code} on {tag}: {e}")
            raise
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            log.warning(f"Network error attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
            else:
                raise


async def _get_url(url: str, params: dict | None = None) -> dict:
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params={"lang": "en", **(params or {})})
    log.info(f"← {r.status_code}  {(time.perf_counter()-t0)*1000:.0f}ms  {url}")
    r.raise_for_status()
    return r.json()

# ── JSON-stat2 parser ──────────────────────────────────────────────────────────

def _parse_jsonstat2(raw: dict, *, table_id: str = "") -> list[dict]:
    ds = raw.get("dataset", raw)
    dims, size, values, dim_ids = ds["dimension"], ds["size"], ds["value"], ds["id"]
    label_maps: dict[str, dict] = {}
    for d_id in dim_ids:
        cats = dims[d_id]["category"]
        idx  = cats["index"]
        ordered = idx if isinstance(idx, list) else sorted(idx, key=idx.__getitem__)
        label_maps[d_id] = {str(i): cats["label"][k] for i, k in enumerate(ordered)}
    for d_id in dim_ids:
        log.info(f"  dim={d_id}  categories={len(label_maps[d_id])}  "
                 f"sample={list(label_maps[d_id].values())[:3]}")
    total = 1
    for s in size: total *= s
    rows, null_count = [], 0
    for flat_idx in range(total):
        coords: dict[str, int] = {}
        rem = flat_idx
        for i in range(len(dim_ids) - 1, -1, -1):
            coords[dim_ids[i]] = rem % size[i]; rem //= size[i]
        val = values[flat_idx]
        if val is None: null_count += 1; continue
        row = {d: label_maps[d][str(coords[d])] for d in dim_ids}
        row["value"] = val
        rows.append(row)
    null_pct = 100 * null_count / total if total else 0
    log.info(f"Parsed  table={table_id}  rows={len(rows)}  nulls={null_count} ({null_pct:.1f}%)")
    store.stats["total_rows_returned"] += len(rows)
    return rows

# ── Tool wrapper helper ────────────────────────────────────────────────────────

def _record_tool(name: str, params: dict, rows: int, elapsed_ms: float, status: str):
    store.add_tool_call({
        "ts":      datetime.now().strftime("%H:%M:%S"),
        "tool":    name,
        "params":  params,
        "rows":    rows,
        "ms":      round(elapsed_ms),
        "status":  status,
    })
    if status == "error":
        store.stats["errors"] += 1

# ── MCP Tools ─────────────────────────────────────────────────────────────────

@mcp.tool(description=(
    "Fetch the SSB house price index for existing dwellings (table 07230). "
    "Returns YEARLY index values broken down by dwelling type. "
    "Use periods='top(5)' for last 5 years, 'top(10)' for last 10 years, "
    "or an explicit range like '2009-2024'."
))
async def get_house_price_index(periods: str = "top(10)") -> dict:
    t = time.perf_counter()
    log.info(f"TOOL get_house_price_index  periods={periods}")
    params = {"valueCodes[Tid]": periods, "valueCodes[Boligtype]": "*",
              "valueCodes[ContentsCode]": "*"}
    try:
        raw  = await _get("07230", params, label="07230-price-index")
        rows = _parse_jsonstat2(raw, table_id="07230")
        ms   = (time.perf_counter() - t) * 1000
        log.info(f"TOOL DONE get_house_price_index  rows={len(rows)}  {ms:.0f}ms")
        _record_tool("get_house_price_index", {"periods": periods}, len(rows), ms, "ok")
        return {"status": "ok", "table": "07230", "rows": rows, "row_count": len(rows)}
    except Exception as e:
        ms = (time.perf_counter() - t) * 1000
        log.error(f"TOOL FAILED get_house_price_index: {e}")
        _record_tool("get_house_price_index", {"periods": periods}, 0, ms, "error")
        return {"status": "error", "message": str(e)}


@mcp.tool(description=(
    "Fetch average square metre prices (NOK/m²) for existing dwellings "
    "by Norwegian county/region (table 06035). "
    "Use periods='top(8)' for 2 years, or '*' for all available data."
))
async def get_sqm_price_by_region(periods: str = "top(8)") -> dict:
    t = time.perf_counter()
    log.info(f"TOOL get_sqm_price_by_region  periods={periods}")
    params = {"valueCodes[Tid]": periods, "valueCodes[Region]": "*",
              "valueCodes[Boligtype]": "*", "valueCodes[ContentsCode]": "KvPris"}
    try:
        raw  = await _get("06035", params, label="06035-sqm-price")
        rows = _parse_jsonstat2(raw, table_id="06035")
        ms   = (time.perf_counter() - t) * 1000
        log.info(f"TOOL DONE get_sqm_price_by_region  rows={len(rows)}  {ms:.0f}ms")
        _record_tool("get_sqm_price_by_region", {"periods": periods}, len(rows), ms, "ok")
        return {"status": "ok", "table": "06035", "rows": rows, "row_count": len(rows)}
    except Exception as e:
        ms = (time.perf_counter() - t) * 1000
        log.error(f"TOOL FAILED get_sqm_price_by_region: {e}")
        _record_tool("get_sqm_price_by_region", {"periods": periods}, 0, ms, "error")
        return {"status": "error", "message": str(e)}


@mcp.tool(description=(
    "List the available time periods for a given SSB table. "
    "Use table_id='07230' (price index) or '06035' (sqm prices)."
))
async def list_available_periods(table_id: str = "07230") -> dict:
    t = time.perf_counter()
    log.info(f"TOOL list_available_periods  table={table_id}")
    try:
        meta = await _get_url(f"{SSB_BASE}/{table_id}/metadata")
        for var in meta.get("variables", []):
            if var.get("type") == "Time" or var.get("id") in ("Tid", "tid"):
                periods = var.get("values", [])
                ms = (time.perf_counter() - t) * 1000
                log.info(f"TOOL DONE list_available_periods  periods={len(periods)}  {ms:.0f}ms")
                _record_tool("list_available_periods", {"table_id": table_id}, len(periods), ms, "ok")
                return {"status": "ok", "table": table_id, "period_count": len(periods),
                        "periods": periods, "first_period": periods[0] if periods else None,
                        "last_period": periods[-1] if periods else None}
        return {"status": "ok", "table": table_id, "note": "No time variable found"}
    except Exception as e:
        ms = (time.perf_counter() - t) * 1000
        log.error(f"TOOL FAILED list_available_periods: {e}")
        _record_tool("list_available_periods", {"table_id": table_id}, 0, ms, "error")
        return {"status": "error", "message": str(e)}


@mcp.tool(description=(
    "Search SSB's full table catalogue. Pass a keyword like 'boligpris', "
    "'housing', 'rent', 'byggeareal' to discover tables beyond 07230 and 06035."
))
async def search_ssb_tables(query: str, max_results: int = 10) -> dict:
    t = time.perf_counter()
    log.info(f"TOOL search_ssb_tables  query='{query}'")
    try:
        data   = await _get_url(SSB_BASE, {"query": query})
        tables = data if isinstance(data, list) else data.get("tables", [])
        results = [{"id": x.get("id"), "title": x.get("title") or x.get("text"),
                    "updated": x.get("updated"), "category": x.get("category")}
                   for x in tables[:max_results]]
        ms = (time.perf_counter() - t) * 1000
        log.info(f"TOOL DONE search_ssb_tables  found={len(results)}  {ms:.0f}ms")
        _record_tool("search_ssb_tables", {"query": query}, len(results), ms, "ok")
        return {"status": "ok", "query": query, "results": results, "count": len(results)}
    except Exception as e:
        ms = (time.perf_counter() - t) * 1000
        log.error(f"TOOL FAILED search_ssb_tables: {e}")
        _record_tool("search_ssb_tables", {"query": query}, 0, ms, "error")
        return {"status": "error", "message": str(e)}

# ── Web UI (port 8003) ─────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SSB MCP Server Monitor</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Inter:wght@400;600;700&display=swap');
  :root{--bg:#0d1117;--surface:#161b22;--border:#30363d;--green:#3fb950;--red:#f85149;--yellow:#d29922;--blue:#58a6ff;--text:#e6edf3;--muted:#8b949e}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:14px;min-height:100vh}
  header{padding:1.2rem 2rem;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:1rem}
  header h1{font-size:1rem;font-weight:700;letter-spacing:.03em;display:flex;align-items:center;gap:.6rem}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green);animation:pulse 2s ease-in-out infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .badge{font-family:'DM Mono',monospace;font-size:.7rem;padding:.2rem .6rem;border-radius:4px;background:#1c2128;border:1px solid var(--border);color:var(--muted)}
  .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;padding:1.2rem 2rem}
  .kpi{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:1rem 1.2rem}
  .kpi-label{font-size:.7rem;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:.5rem}
  .kpi-value{font-size:1.8rem;font-weight:700;font-family:'DM Mono',monospace;color:var(--blue)}
  .main{display:grid;grid-template-columns:1fr 1fr;gap:1rem;padding:0 2rem 2rem}
  .panel{background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden}
  .panel-header{padding:.7rem 1rem;border-bottom:1px solid var(--border);font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);display:flex;justify-content:space-between;align-items:center}
  .clear-btn{font-family:'DM Mono',monospace;font-size:.65rem;background:transparent;border:1px solid var(--border);color:var(--muted);padding:.15rem .5rem;border-radius:3px;cursor:pointer}
  .clear-btn:hover{border-color:var(--red);color:var(--red)}
  /* Log panel */
  #log-panel{height:380px;overflow-y:auto;padding:.5rem .8rem;font-family:'DM Mono',monospace;font-size:.72rem;line-height:1.6}
  .log-line{display:flex;gap:.6rem;padding:.1rem 0;border-bottom:1px solid #1c2128}
  .log-ts{color:var(--muted);flex-shrink:0;width:80px}
  .log-INFO .log-level{color:var(--blue)}
  .log-WARNING .log-level{color:var(--yellow)}
  .log-ERROR .log-level{color:var(--red)}
  .log-DEBUG .log-level{color:var(--muted)}
  .log-level{flex-shrink:0;width:52px}
  .log-msg{color:var(--text);word-break:break-all}
  /* Tool call table */
  #tool-panel{height:380px;overflow-y:auto}
  table{width:100%;border-collapse:collapse;font-family:'DM Mono',monospace;font-size:.72rem}
  th{padding:.5rem .8rem;text-align:left;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--surface)}
  td{padding:.45rem .8rem;border-bottom:1px solid #1c2128;vertical-align:top}
  tr:hover td{background:#1c2128}
  .status-ok{color:var(--green)}
  .status-error{color:var(--red)}
  .tool-name{color:var(--blue)}
  .rows-val{color:var(--green)}
  .ms-val{color:var(--yellow)}
  /* Uptime */
  #uptime{font-family:'DM Mono',monospace;font-size:.8rem;color:var(--muted)}
  @media(max-width:900px){
    .grid{grid-template-columns:1fr 1fr}
    .main{grid-template-columns:1fr}
  }
</style>
</head>
<body>
<header>
  <h1><span class="dot"></span> SSB MCP Server Monitor</h1>
  <div style="display:flex;gap:.6rem;align-items:center;flex-wrap:wrap">
    <span class="badge">:8002/mcp — MCP endpoint</span>
    <span class="badge">:8003 — this UI</span>
    <span id="uptime">up 0s</span>
  </div>
</header>

<div class="grid">
  <div class="kpi"><div class="kpi-label">Tool Calls</div><div class="kpi-value" id="s-calls">0</div></div>
  <div class="kpi"><div class="kpi-label">SSB Requests</div><div class="kpi-value" id="s-reqs">0</div></div>
  <div class="kpi"><div class="kpi-label">Rows Returned</div><div class="kpi-value" id="s-rows">0</div></div>
  <div class="kpi"><div class="kpi-label">Errors</div><div class="kpi-value" id="s-errors" style="color:var(--red)">0</div></div>
</div>

<div class="main">
  <div class="panel">
    <div class="panel-header">
      Live Logs
      <button class="clear-btn" onclick="clearLogs()">clear</button>
    </div>
    <div id="log-panel"></div>
  </div>
  <div class="panel">
    <div class="panel-header">Tool Calls</div>
    <div id="tool-panel">
      <table>
        <thead><tr><th>Time</th><th>Tool</th><th>Params</th><th>Rows</th><th>ms</th><th>Status</th></tr></thead>
        <tbody id="tool-tbody"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
const logPanel  = document.getElementById('log-panel');
const toolTbody = document.getElementById('tool-tbody');
const startedAt = new Date();

// ── uptime counter ──
setInterval(() => {
  const s = Math.floor((Date.now() - startedAt) / 1000);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
  document.getElementById('uptime').textContent =
    `up ${h ? h+'h ' : ''}${m ? m+'m ' : ''}${sec}s`;
}, 1000);

// ── SSE stream ──
const es = new EventSource('/events');
es.onmessage = e => {
  const d = JSON.parse(e.data);
  if (d.type === 'log')       addLog(d);
  if (d.type === 'tool_call') addToolCall(d);
  if (d.type === 'stats')     updateStats(d);
};
es.onerror = () => {
  addLog({ts: new Date().toLocaleTimeString(), level: 'WARNING',
          msg: 'SSE connection lost — reconnecting…'});
};

// ── initial state ──
fetch('/state').then(r=>r.json()).then(d => {
  d.logs.forEach(addLog);
  d.tool_calls.slice().reverse().forEach(addToolCall);
  updateStats(d.stats);
});

function addLog(d) {
  const div = document.createElement('div');
  div.className = `log-line log-${d.level}`;
  div.innerHTML = `<span class="log-ts">${d.ts}</span>`
    + `<span class="log-level">${d.level}</span>`
    + `<span class="log-msg">${escHtml(d.msg)}</span>`;
  logPanel.appendChild(div);
  logPanel.scrollTop = logPanel.scrollHeight;
  // keep DOM lean
  while (logPanel.children.length > 300) logPanel.removeChild(logPanel.firstChild);
}

function addToolCall(d) {
  const tr = document.createElement('tr');
  const params = Object.entries(d.params||{}).map(([k,v])=>`${k}=${v}`).join(' ');
  tr.innerHTML = `<td>${d.ts}</td>`
    + `<td class="tool-name">${escHtml(d.tool)}</td>`
    + `<td style="color:var(--muted)">${escHtml(params)}</td>`
    + `<td class="rows-val">${d.rows}</td>`
    + `<td class="ms-val">${d.ms}</td>`
    + `<td class="status-${d.status}">${d.status}</td>`;
  toolTbody.insertBefore(tr, toolTbody.firstChild);
  while (toolTbody.children.length > 100) toolTbody.removeChild(toolTbody.lastChild);
}

function updateStats(s) {
  document.getElementById('s-calls').textContent  = s.total_tool_calls;
  document.getElementById('s-reqs').textContent   = s.total_ssb_requests;
  document.getElementById('s-rows').textContent   = s.total_rows_returned;
  document.getElementById('s-errors').textContent = s.errors;
}

function clearLogs() { logPanel.innerHTML = ''; }
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
</script>
</body>
</html>"""


async def _web_ui():
    """Tiny aiohttp server serving the dashboard on UI_PORT."""
    from aiohttp import web

    async def index(req):
        return web.Response(text=HTML, content_type="text/html")

    async def state(req):
        return web.json_response({
            "logs":       list(store.logs),
            "tool_calls": list(store.tool_calls),
            "stats":      store.stats,
        })

    async def events(req):
        """SSE endpoint — streams live events to the browser."""
        resp = web.StreamResponse(headers={
            "Content-Type":  "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection":    "keep-alive",
            "Access-Control-Allow-Origin": "*",
        })
        await resp.prepare(req)
        q = store.subscribe()
        try:
            while True:
                event = await asyncio.wait_for(q.get(), timeout=25)
                data  = json.dumps(event)
                await resp.write(f"data: {data}\n\n".encode())
        except (asyncio.TimeoutError, ConnectionResetError):
            pass
        finally:
            store.unsubscribe(q)
        return resp

    app = web.Application()
    app.router.add_get("/",       index)
    app.router.add_get("/state",  state)
    app.router.add_get("/events", events)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", UI_PORT).start()
    log.info(f"Web UI running → http://localhost:{UI_PORT}")


# ── Entry point ────────────────────────────────────────────────────────────────

async def _main():
    await _web_ui()
    log.info("━" * 55)
    log.info("  SSB Housing MCP Server")
    log.info(f"  MCP endpoint : http://localhost:{MCP_PORT}/mcp")
    log.info(f"  Web UI       : http://localhost:{UI_PORT}")
    log.info("  Tools        : get_house_price_index | get_sqm_price_by_region")
    log.info("               : list_available_periods | search_ssb_tables")
    log.info("━" * 55)
    # Run FastMCP in the same event loop
    await mcp.run_async(transport="streamable-http", host="0.0.0.0", port=MCP_PORT)

if __name__ == "__main__":
    asyncio.run(_main())
from __future__ import annotations
import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .jsonrpc import JSONRPCRequest, make_error, make_result
from .tools import tool_definitions, TOOL_HANDLERS
from .oic_client import oic_client_singleton as oic

# Setup logging.
# Was a plain FileHandler with no size cap - mcp_server.log grew unbounded
# forever (this is why the file was flagged as a large-file risk). A
# RotatingFileHandler caps it at 10MB per file and keeps 5 rotated backups
# (~60MB ceiling total), automatically, with no cron/logrotate/sudo needed on
# either Windows or Linux - the app manages its own log size on every write.
_LOG_FILE = os.environ.get("MCP_LOG_FILE", "mcp_server.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def format_mcp_response(result: Any) -> Dict[str, Any]:
    """
    Format OIC API responses into clean MCP-compatible structure.
    Handles the OIC API's content wrapper and standardizes the response format.
    """
    if not isinstance(result, dict):
        return result
    
    # Handle OIC API responses that have a 'content' wrapper
    if 'content' in result and isinstance(result['content'], dict):
        content = result['content']
        # Extract items, totalResults, hasMore from content
        formatted = {}
        if 'items' in content:
            formatted['items'] = content['items']
        if 'totalResults' in content:
            formatted['totalResults'] = content['totalResults']
        if 'hasMore' in content:
            formatted['hasMore'] = content['hasMore']
        if 'links' in content:
            formatted['links'] = content['links']
        return formatted
    
    # Handle direct responses (no content wrapper) - already clean format
    if 'items' in result or 'totalResults' in result:
        return result
    
    # For single object responses (like get_integration, get_connection, etc.)
    # Return as-is since they're already in the right format
    return result


def should_use_standard_response(result: Any) -> bool:
    """
    Determine if we should wrap the result in a standard response format.
    """
    if not isinstance(result, dict):
        return False
    
    # For list-type responses (integrations, connections, etc.), use standard format
    if 'items' in result or 'totalResults' in result:
        return True
    
    # For single object responses, return as-is for simplicity
    return False


def create_standard_response(data: Any, message: str = "Success", count: int = None) -> Dict[str, Any]:
    """
    Create a standardized MCP response format for consistent client consumption.
    """
    response = {
        "data": data,
        "message": message,
        "success": True
    }
    
    if count is not None:
        response["count"] = count
    elif isinstance(data, list):
        response["count"] = len(data)
    elif isinstance(data, dict) and "items" in data:
        response["count"] = len(data["items"])
    
    return response


app = FastAPI(title="OIC Monitoring MCP Server", version="0.1.0")

MCP_PROTOCOL_VERSION = "2025-06-18"

# Maximum characters of serialized tool payload sent to the client. Some
# tools (list_all_integrations, search_*) can return thousands of records;
# without a cap a single call would flood the client's context. Truncation
# is always announced in the text, never silent.
MAX_TOOL_TEXT_CHARS = 100_000


def to_mcp_tool_result(payload: Any, is_error: bool = False) -> Dict[str, Any]:
    """
    Wrap a tool payload in the MCP `tools/call` result envelope.

    The MCP spec requires the result of tools/call to be
    {"content": [{"type": "text", "text": ...}], "isError": bool}.
    Returning the bare domain object (e.g. {"data": ..., "success": ...})
    parses as valid JSON-RPC but carries no `content` key, so a spec-
    compliant client reads it as an empty response and reports the call as
    producing no output - with no error anywhere to explain why.
    """
    if isinstance(payload, str):
        text = payload
    else:
        try:
            text = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(payload)

    if len(text) > MAX_TOOL_TEXT_CHARS:
        omitted = len(text) - MAX_TOOL_TEXT_CHARS
        text = (
            text[:MAX_TOOL_TEXT_CHARS]
            + f"\n\n[TRUNCATED by MCP server: {omitted} of "
            f"{omitted + MAX_TOOL_TEXT_CHARS} characters omitted. Re-run with "
            f"a narrower filter or a smaller limit to see the rest.]"
        )

    return {"content": [{"type": "text", "text": text}], "isError": is_error}


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    # Close the shared OIC client once, when the whole process is stopping -
    # NOT per-websocket-connection (see the removed per-connection aclose()
    # below). The client is a module-level singleton shared by every
    # concurrent connection; closing it when any one connection ends broke
    # every other connection still using it, including future ones, since
    # nothing recreated it automatically after that.
    await oic.aclose()


@app.get("/healthz")
async def healthz() -> Any:
    return JSONResponse({"status": "ok"})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    # Negotiate the 'mcp' subprotocol if the client offered it (Claude Code
    # and other spec-compliant MCP clients send
    # Sec-WebSocket-Protocol: mcp on the handshake and expect it echoed
    # back). Accepting with no subprotocol at all caused strict clients to
    # treat the handshake as a mismatch and refuse to use the connection,
    # even though the raw WebSocket upgrade itself succeeded.
    requested_subprotocols = websocket.scope.get("subprotocols") or []
    subprotocol = "mcp" if "mcp" in requested_subprotocols else None
    await websocket.accept(subprotocol=subprotocol)
    logger.info(f"WebSocket connection accepted (subprotocol={subprotocol!r})")
    
    try:
        while True:
            raw = await websocket.receive_text()
            request_start_time = time.time()
            
            try:
                req: JSONRPCRequest = json.loads(raw)
                method = req.get("method")
                id_value = req.get("id")
                params = req.get("params") or {}
                if not isinstance(params, dict):
                    params = {}

                logger.info(f"Received MCP request: {method} (ID: {id_value})")
                logger.debug(f"Request params: {json.dumps(params, indent=2)}")

                if method == "initialize":
                    logger.info("Processing initialize request")
                    result = {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {
                            # MCP spec requires capability VALUES to be
                            # objects, not booleans - e.g. {} or
                            # {"listChanged": false} to declare the
                            # capability is present, and the key omitted
                            # entirely (not "false") to declare it absent.
                            # Sending booleans here is what a strict client
                            # rejects even though the JSON parses fine.
                            "tools": {"listChanged": False},
                        },
                        "serverInfo": {
                            "name": "oic-monitoring-mcp",
                            "version": "0.1.0",
                        },
                    }
                    await websocket.send_text(json.dumps(make_result(id_value, result)))
                    logger.info("Initialize request completed")


                elif method == "tools/list":
                    logger.info("Processing tools/list request")
                    tools = tool_definitions()
                    await websocket.send_text(json.dumps(make_result(id_value, {"tools": tools})))
                    logger.info("Tools list request completed")

                elif method == "tools/call":
                    name = params.get("name")
                    arguments = params.get("arguments") or {}
                    if not name or name not in TOOL_HANDLERS:
                        error_msg = f"Unknown tool: {name}"
                        logger.error(error_msg)
                        await websocket.send_text(json.dumps(make_error(id_value, -32601, error_msg)))
                        continue
                    
                    logger.info(f"Executing tool: {name}")
                    logger.debug(f"Tool arguments: {json.dumps(arguments, indent=2)}")
                    
                    tool_start_time = time.time()
                    try:
                        result = await TOOL_HANDLERS[name](arguments)
                        tool_execution_time = time.time() - tool_start_time
                        
                        logger.info(f"Tool {name} executed successfully in {tool_execution_time:.2f}s")
                        logger.debug(f"Tool result keys: {list(result.keys()) if isinstance(result, dict) else 'Not a dict'}")
                        
                        # Format the response to handle OIC API structure internally
                        format_start_time = time.time()
                        formatted_result = format_mcp_response(result)
                        format_time = time.time() - format_start_time
                        
                        logger.debug(f"Response formatting took {format_time:.2f}s")
                        
                        # Use standard response format for list-type responses
                        if should_use_standard_response(formatted_result):
                            final_result = create_standard_response(formatted_result, f"Successfully executed {name}")
                        else:
                            # For single object responses, return as-is
                            final_result = formatted_result
                        
                        await websocket.send_text(json.dumps(make_result(id_value, to_mcp_tool_result(final_result))))
                        
                        total_request_time = time.time() - request_start_time
                        logger.info(f"Tool {name} request completed in {total_request_time:.2f}s total")
                        
                    except Exception as e:  # noqa: BLE001
                        tool_execution_time = time.time() - tool_start_time
                        error_msg = f"Tool execution error: {str(e)}"
                        logger.error(f"Tool {name} failed after {tool_execution_time:.2f}s: {e}")
                        await websocket.send_text(json.dumps(make_result(id_value, to_mcp_tool_result(error_msg, is_error=True))))

                else:
                    error_msg = f"Unknown method: {method}"
                    logger.error(error_msg)
                    await websocket.send_text(json.dumps(make_error(id_value, -32601, error_msg)))

            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error: {e}")
                await websocket.send_text(json.dumps(make_error(None, -32700, "Parse error")))
            except Exception as e:
                logger.error(f"Unexpected error processing request: {e}")
                await websocket.send_text(json.dumps(make_error(None, -32603, f"Internal error: {str(e)}")))
                
    except WebSocketDisconnect:
        logger.info("WebSocket connection disconnected")
        return
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    # NOTE: no oic.aclose() here anymore - see the app "shutdown" handler
    # above. This is a per-connection handler; the OIC client is a
    # process-wide singleton and must outlive any single connection.


if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8085"))
    uvicorn.run(app, host=host, port=port, ws="websockets")

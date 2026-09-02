"""Thin MCP server entry point.

    agent-backend-mcp [--workspace DIR]

The server validates protocol input (handled by the MCP SDK from the tool
signatures), invokes the backend, and returns the backend's structured
response. No business logic lives here.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from mcp.server.mcpserver import MCPServer

from ...core.backend import Backend
from ..tools import register_tools

INSTRUCTIONS = """\
This server is an agent-ready data backend. Express what you want, not how to do it:
- Reference datasets by name, alias, id, or a loose description ("yesterday's sales").
- Field names may be approximate; responses report how they were resolved.
- If a response has status "needs_resolution", pick one of the candidates and retry.
- If a response has status "error", read `code`, `message` and `advice`: each advice entry says what the backend accepts instead of what you wrote and, when the fix is mechanical, carries `rewrite`, your request as tool calls to send as-is; `candidates` lists what a name could have meant. Most errors are recoverable by fixing the intent.
- A successful response may carry `advice` too: an empty result says where a filtered value actually occurs; a result that already has the declared output shape says so and names the next call.
- State-changing tools are safe to retry; identical requests replay the original result.
- Declare the shape of your deliverable with declare_output while the requirement is in front of you; export_result holds the file to it.
The backend owns identifiers, storage layout, versions, lineage, audit and transactions.
"""


def create_server(backend: Backend) -> MCPServer:
    server = MCPServer(name="agent-backend", instructions=INSTRUCTIONS, version="0.1.0")
    register_tools(server, backend)
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Agent-ready backend MCP server")
    parser.add_argument("--workspace", default=os.environ.get("AGENT_BACKEND_WORKSPACE", "./workspace"),
                        help="Managed workspace directory (default: ./workspace or $AGENT_BACKEND_WORKSPACE)")
    parser.add_argument("--export-root", default=os.environ.get("AGENT_BACKEND_EXPORT_ROOT"),
                        help="Directory exports may be written to (default: <workspace>/exports)")
    parser.add_argument("--log-level", default=os.environ.get("AGENT_BACKEND_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    export_root = args.export_root or os.path.join(args.workspace, "exports")
    backend = Backend(args.workspace, export_root=export_root)
    try:
        create_server(backend).run(transport="stdio")
    finally:
        backend.close()


if __name__ == "__main__":
    main()

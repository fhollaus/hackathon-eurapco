"""Nine domain tools, available over stdio or local Streamable HTTP (MCP 2.2)."""

import argparse
import json

from mcp import types
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools import Tool

from app.contracts import TOOLS, tool_schema
from app.domain import DomainStore


def build_server():
    store = DomainStore()

    class DomainTool(Tool):
        async def run(self, arguments, context, convert_result=False):
            # Keep schema errors in the shared result envelope; avoid SDK coercion.
            result = store.call(self.name, arguments)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(result))],
                structured_content=result, is_error=not result["ok"],
            )

    def operation(**arguments) -> dict:
        return {}  # Registration metadata only. DomainTool.run owns execution.

    tools = []
    for name, (_, description) in TOOLS.items():
        tool = DomainTool.from_function(operation, name=name, description=description, structured_output=False)
        tool.parameters = tool_schema(name)
        tools.append(tool)
    return MCPServer("Eurapco medical policy", version="0.1.0", tools=tools, log_level="WARNING")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = build_server()
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport="streamable-http", host="127.0.0.1", port=args.port,
                   streamable_http_path="/mcp", json_response=True, stateless_http=True)


if __name__ == "__main__":
    main()

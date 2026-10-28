"""Provided MCP connection, bound run context, per-agent capabilities and capture."""

import json
import os
import sys

from mcp import Client, StdioServerParameters

from app.contracts import ACCESS, ROOT, TOOLS, tool_schema
from app.domain import error


class DomainClient:
    def __init__(self, run_id, agent, log=None, url=None):
        self.run_id, self.agent, self.log = run_id, agent, log
        self.url = url or os.environ.get("MCP_URL")

    async def __aenter__(self):
        server_env = {"EURAPCO_DATA_DIR": os.environ["EURAPCO_DATA_DIR"]} if os.environ.get("EURAPCO_DATA_DIR") else {}
        transport = self.url or StdioServerParameters(command=sys.executable, args=["-m", "mcp_server"], cwd=ROOT, env=server_env)
        self.client = Client(transport, read_timeout_seconds=30)
        await self.client.__aenter__()
        try:
            listing = await self.client.list_tools()
            if {t.name for t in listing.tools} != set(TOOLS):
                raise RuntimeError("Server does not expose exactly the nine supplied tools.")
        except BaseException:
            await self.client.__aexit__(*sys.exc_info())
            raise
        return self

    async def __aexit__(self, *args):
        return await self.client.__aexit__(*args)

    def model_tools(self):
        return [{"name": name, "description": TOOLS[name][1], "input_schema": tool_schema(name, model_facing=True)}
                for name in ACCESS[self.agent]]

    async def call(self, name, arguments):
        payload = {**arguments, "run_id": self.run_id}
        if self.log:
            # Log what the caller sent: a model-supplied run_id must stay visible, not be masked by the bound one.
            self.log.event("tool_call", name=name, arguments={"run_id": self.run_id, **arguments})
        if name not in ACCESS[self.agent]:
            result = error("INVALID_ARGUMENT", "Tool is not available to this agent.")
        elif "run_id" in arguments:
            result = error("INVALID_ARGUMENT", "run_id is bound by the client; omit it.", field="run_id", retryable=True)
        else:
            response = await self.client.call_tool(name, payload)
            result = response.structured_content
            if result is None:
                text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
                try:
                    result = json.loads(text)
                except (ValueError, TypeError):
                    raise RuntimeError("MCP response did not contain the shared tool result envelope.") from None
            if not isinstance(result, dict) or "ok" not in result:
                raise RuntimeError("Unexpected MCP tool result.")
        if self.log:
            self.log.event("tool_result", name=name, result=result)
        return result

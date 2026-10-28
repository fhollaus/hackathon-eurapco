"""Small working baseline agent loop. No persistent human-loop implementation."""

import json
import os

from anthropic import AsyncAnthropic

from agents.administration.agent import system_prompt as admin_prompt
from agents.claims.agent import system_prompt as claims_prompt
from app.contracts import document, validator
from app.mcp_client import DomainClient
from app.router import route


async def run_request(request, run_id, log, provider=None):
    validator("events.schema.json").validate(request)
    if request["event_type"] != "request":
        raise ValueError("The baseline handles initial requests only; teams implement human reply/resumption.")
    agent = route(request["text"])
    log.route(agent)
    prompt = admin_prompt() if agent == "administration" else claims_prompt()
    prompt += "\nReturn ONLY one JSON object matching this response schema when done or waiting:\n"
    prompt += json.dumps(document("agent-response.schema.json"))
    # No prewritten skills or solved skill integration in the initial agent.
    messages = [{"role": "user", "content": json.dumps(request)}]
    owned = provider is None
    provider = provider or AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=0, timeout=45)
    try:
        async with DomainClient(run_id, agent, log) as domain:
            for _ in range(12):
                response = await provider.messages.create(
                    model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5"), max_tokens=1800,
                    system=prompt, messages=messages, tools=domain.model_tools(),
                )
                calls = [b for b in response.content if b.type == "tool_use"]
                if calls:
                    messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in response.content]})
                    results = []
                    for call in calls:
                        result = await domain.call(call.name, call.input)
                        results.append({"type": "tool_result", "tool_use_id": call.id,
                                        "content": json.dumps(result), "is_error": not result["ok"]})
                    messages.append({"role": "user", "content": results})
                    continue
                text = "".join(b.text for b in response.content if b.type == "text").strip()
                if text.startswith("```json") and text.endswith("```"):
                    text = text[7:-3].strip()
                final = json.loads(text)
                validator("agent-response.schema.json").validate(final)
                if final["request_category"] not in document("vocabulary.json")["request_categories"][agent]:
                    raise ValueError("Agent returned a category from the other domain.")
                log.event("model_response", response=final)
                if final["agent_state"] == "awaiting_clarification":
                    log.human_requested("clarification", {"question": final["question"]})
                elif final["agent_state"] == "awaiting_authorization":
                    log.human_requested("authorization", final["proposal"])
                log.status(agent_state=final["agent_state"], request_outcome=final["request_outcome"],
                           reason=final["reason"], message=final["message"], request_category=final["request_category"])
                return final
        raise RuntimeError("Baseline turn budget exhausted. Stop and hand over; do not repeat successful writes.")
    finally:
        if owned:
            await provider.close()

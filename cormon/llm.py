"""OpenAI wrapper: structured JSON output, temperature 0, cached by input hash.

The cache is what makes the same message reach the same judgment on repeated
runs; `use_cache=False` forces a fresh call (operator re-run).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from . import db, settings

_client = None


def client():
    global _client
    if _client is None:
        from openai import OpenAI

        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set (put it in .env)")
        _client = OpenAI()
    return _client


def model_for(task: str) -> str:
    return settings.get(f"llm.models.{task}") or settings.get("llm.models.default", "gpt-4.1")


def structured(
    task: str,
    system: str,
    user: str,
    schema: dict,
    schema_name: str,
    *,
    use_cache: bool = True,
    model: str | None = None,
) -> dict:
    model = model or model_for(task)
    temperature = settings.get("llm.temperature", 0)
    seed = settings.get("llm.seed", 7)
    key = hashlib.sha256(
        json.dumps([model, temperature, system, user, schema], sort_keys=True).encode()
    ).hexdigest()
    if use_cache:
        row = db.q1("SELECT response FROM llm_cache WHERE key=?", (key,))
        if row:
            return json.loads(row["response"])

    kwargs: dict[str, Any] = dict(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        },
    )
    if not model.startswith(("gpt-5", "o")):
        kwargs["temperature"] = temperature
        kwargs["seed"] = seed

    last_err: Exception | None = None
    for attempt in range(4):
        try:
            resp = client().chat.completions.create(**kwargs)
            data = json.loads(resp.choices[0].message.content)
            break
        except Exception as e:  # network, rate limit, malformed output
            last_err = e
            time.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError(f"LLM call failed for {task}: {last_err}")

    db.ex(
        "INSERT OR REPLACE INTO llm_cache(key, model, response, created_at) VALUES (?,?,?,?)",
        (key, model, json.dumps(data), db.now()),
    )
    return data


def obj(properties: dict, required: list[str] | None = None) -> dict:
    """Strict-mode object schema helper: every property required, no extras."""
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


def nullable(t: str | dict) -> dict:
    if isinstance(t, str):
        return {"type": [t, "null"]}
    return {"anyOf": [t, {"type": "null"}]}

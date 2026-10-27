"""OpenAI wrapper: structured JSON output, temperature 0, cached by input hash.

The cache is what makes the same message reach the same judgment on repeated
runs; `use_cache=False` forces a fresh call (operator re-run).
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import threading
import time
from typing import Any

from . import db, settings, timing
from .compliance import ailog

_client = None


def client():
    global _client
    if _client is None:
        from openai import OpenAI

        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set (put it in .env)")
        # one slow call must not stall the listener: give up after a while and let structured() retry
        _client = OpenAI(timeout=settings.get("llm.timeout_seconds", 90), max_retries=0)
    return _client


# tokens used by calls that reached the model (cache hits use none), by model: for cost reports
USAGE: dict[str, dict[str, int]] = {}


def _count(model: str, usage) -> None:
    if usage is None:
        return
    u = USAGE.setdefault(model, {"calls": 0, "input": 0, "cached_input": 0, "output": 0})
    u["calls"] += 1
    u["input"] += getattr(usage, "prompt_tokens", 0) or 0
    details = getattr(usage, "prompt_tokens_details", None)
    u["cached_input"] += (getattr(details, "cached_tokens", 0) or 0) if details else 0
    u["output"] += getattr(usage, "completion_tokens", 0) or 0


def model_for(task: str) -> str:
    """CORMON_MODEL_<TASK> overrides one task's model and CORMON_MODEL every task's, for comparing models on a test
    run (CORMON_EFFORT likewise overrides llm.effort.<task> and llm.reasoning_effort for reasoning models)."""
    return (os.environ.get(f"CORMON_MODEL_{task.upper()}") or os.environ.get("CORMON_MODEL")
            or settings.get(f"llm.models.{task}") or settings.get("llm.models.default", "gpt-4.1"))


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
    reasoning = not model.startswith(("gpt-4", "gpt-3"))
    effort = (os.environ.get("CORMON_EFFORT") or settings.get(f"llm.effort.{task}")
              or settings.get("llm.reasoning_effort")) if reasoning else None
    key = hashlib.sha256(
        json.dumps([model, temperature, system, user, schema] + ([effort] if effort else []),
                   sort_keys=True).encode()
    ).hexdigest()
    log = functools.partial(ailog.record, task, "chat.completions", model,
                            {"effort": effort} if reasoning else {"temperature": temperature, "seed": seed},
                            {"system": system, "user": user, "schema_name": schema_name, "schema": schema})
    mine = None
    while use_cache:
        row = db.q1("SELECT response FROM llm_cache WHERE key=?", (key,))
        if row:
            timing.llm_call(task, model, 0, cached=True)
            log(row["response"], cached=True)
            return json.loads(row["response"])
        with _flying_lock:
            other = _flying.get(key)
            if other is None:
                mine = _flying[key] = threading.Event()
        if mine:
            break
        other.wait(settings.get("llm.timeout_seconds", 90) * 4)
    try:
        data = _call(task, model, system, user, schema, schema_name, key, temperature, seed, reasoning, effort)
    except Exception as e:
        log(None, cached=False, ok=False, error=str(e)[:500])
        backup = settings.get("llm.fallback")
        if not backup or backup == model:
            raise
        _fell_back(task, model, backup, e)
        return structured(task, system, user, schema, schema_name, use_cache=use_cache, model=backup)
    else:
        log(json.dumps(data), cached=False)
        return data
    finally:
        if mine:
            with _flying_lock:
                _flying.pop(key, None)
            mine.set()


def _fell_back(task: str, model: str, backup: str, error: Exception) -> None:
    from .compliance import alerts
    db.audit("llm_fallback", entity_type="llm", entity_id=task, model=model, used=backup, error=str(error)[:300])
    alerts.notify(f"model:{model}", f"Study monitor: the AI model {model} is failing",
                  f"Calls to {model} failed after every retry ({str(error)[:300]}), so the bot used the backup "
                  f"model {backup} for '{task}'. Answers from the backup model have not been tested as well. "
                  "Check the model, or change llm.models in the settings.")


def configured_models() -> set[str]:
    models = set((settings.get("llm.models") or {}).values())
    models |= {m for m in (settings.get("llm.fallback"), settings.get("llm.models.decisions")) if m}
    return {m for m in models if isinstance(m, str)}


def check_models(log=print) -> list[str] | None:
    """Once a week: every model the settings name is still offered (a model being retired disappears from the
    list). A missing one is recorded and the admins are told."""
    from datetime import datetime, timezone

    from .compliance import alerts
    week = datetime.now(timezone.utc).strftime("%G-W%V")
    if db.get_state("models_week") == week:
        return None
    db.set_state("models_week", week)
    offered = {m.id for m in client().models.list()}
    gone = sorted(m for m in configured_models() if m not in offered)
    db.audit("models_checked", entity_type="llm", entity_id="models", configured=sorted(configured_models()),
             missing=gone)
    if gone:
        alerts.notify("models", "Study monitor: an AI model in the settings is no longer offered",
                      f"OpenAI no longer lists: {', '.join(gone)}. Calls to it will fail soon or already do. "
                      "Pick a replacement, run the test sets with it, then change llm.models.",
                      every_minutes=7 * 24 * 60)
    log(f"models checked: {len(configured_models())} configured, {len(gone)} not offered")
    return gone


# a call started early (to save time) and the same call made later wait for one answer instead of asking twice
_flying: dict[str, threading.Event] = {}
_flying_lock = threading.Lock()


def _call(task, model, system, user, schema, schema_name, key, temperature, seed, reasoning, effort) -> dict:
    kwargs: dict[str, Any] = dict(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        },
    )
    if not reasoning:                          # newer reasoning models take no temperature or seed
        kwargs["temperature"] = temperature
        kwargs["seed"] = seed
    elif effort:
        kwargs["reasoning_effort"] = effort

    last_err: Exception | None = None
    began = time.perf_counter()
    for attempt in range(4):
        try:
            resp = client().chat.completions.create(**kwargs)
            data = json.loads(resp.choices[0].message.content)
            _count(model, getattr(resp, "usage", None))
            break
        except Exception as e:  # network, rate limit, malformed output
            last_err = e
            time.sleep(2 * (attempt + 1))
    else:
        timing.llm_call(task, model, time.perf_counter() - began, cached=False, attempts=4, ok=False)
        raise RuntimeError(f"LLM call failed for {task}: {last_err}")
    timing.llm_call(task, model, time.perf_counter() - began, cached=False, usage=getattr(resp, "usage", None),
                    attempts=attempt + 1)

    db.ex(
        "INSERT OR REPLACE INTO llm_cache(key, model, response, created_at) VALUES (?,?,?,?)",
        (key, model, json.dumps(data), db.now()),
    )
    return data


def decide(task: str, input: str | list[dict], questions: list[dict], *, use_cache: bool = True) -> dict:
    """The Decisions API (fast, bounded answers): {question name: probability for a predicate, value for a choice}.
    Cached by input hash like structured(). Raises after 3 failed attempts."""
    model = settings.get("llm.models.decisions", "gpt-6-luna")
    key = hashlib.sha256(json.dumps(["decisions", model, input, questions], sort_keys=True).encode()).hexdigest()
    log = functools.partial(ailog.record, task, "decisions", model, {}, {"input": input, "questions": questions})
    if use_cache:
        row = db.q1("SELECT response FROM llm_cache WHERE key=?", (key,))
        if row:
            timing.llm_call(task, model, 0, cached=True)
            log(row["response"], cached=True)
            return json.loads(row["response"])
    began, last_err = time.perf_counter(), None
    for attempt in range(3):
        try:
            d = client().decisions.create(model=model, input=input, questions=questions)
            out = {}
            for a in d.answers:
                if a.type == "refusal":
                    raise RuntimeError(f"refused: {a.name}")
                out[a.name] = a.probability if a.type == "predicate" else getattr(a, "choice", None)
            break
        except Exception as e:
            last_err = e
            time.sleep(attempt + 1)
    else:
        timing.llm_call(task, model, time.perf_counter() - began, cached=False, attempts=3, ok=False)
        log(None, cached=False, ok=False, error=str(last_err)[:500])
        raise RuntimeError(f"Decisions call failed for {task}: {last_err}")
    timing.llm_call(task, model, time.perf_counter() - began, cached=False, attempts=attempt + 1)
    db.ex("INSERT OR REPLACE INTO llm_cache(key, model, response, created_at) VALUES (?,?,?,?)",
          (key, model, json.dumps(out), db.now()))
    log(json.dumps(out), cached=False)
    return out


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

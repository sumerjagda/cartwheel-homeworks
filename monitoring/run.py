"""Post-deployment monitoring runner for Homework 7.

Supports two execution modes:
  1. Comparison period mode (Parts A-C):
       uv run --frozen python -m monitoring.run --period before
       uv run --frozen python -m monitoring.run --period after
  2. Scheduled window mode (Part D):
       uv run --frozen python -m monitoring.run --last-hours 24

Fetches traces from Langfuse, validates scenario IDs and Cartwheel model,
groups multi-turn traces into one conversation record per scenario (or per
``meta.session_id`` in ``--last-hours`` mode) using the final trace ID as the
record ID, prepares the exact Homework 5 normalized conversation text, samples
a uniform random subset separately from risk groups, runs the frozen Homework 5
judge once over the union, computes Rogan-Gladen corrected prevalence over the
random sample only, writes idempotent Langfuse scores, and updates
``monitoring/history.jsonl`` and ``monitoring/prevalence.svg``.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from analysis.helpers.normalization import _flatten
from monitoring.chart import prevalence_chart
from monitoring.correct import corrected_mode_prevalence
from monitoring.run_judges import judge_sample, judge_test_data, load_monitoring_judge
from monitoring.sample import DEFAULT_RISK_GROUPS, select_traces
from monitoring.write_scores import build_score_records, post_scores
from observability.instrument import load_env

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "monitoring" / "config.json"
DEFAULT_SCENARIOS_PATH = REPO_ROOT / "scenarios" / "monitoring_scenarios.jsonl"
DEFAULT_HISTORY_PATH = REPO_ROOT / "monitoring" / "history.jsonl"
DEFAULT_CHART_PATH = REPO_ROOT / "monitoring" / "prevalence.svg"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "monitoring" / "out"

RETRY_BACKOFF_SECONDS = (5, 15, 30, 60)
TRANSIENT_ERROR_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "rate limit",
    "ratelimit",
    "resource_exhausted",
    "service unavailable",
    "temporarily unavailable",
    "timeout",
    "timed out",
    "connection reset",
    "connection error",
)


# ---------------------------------------------------------------------------
# Trace unpacking helpers (mirrors HW5 normalized conversation format)
# ---------------------------------------------------------------------------


def _as_plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if hasattr(value, "json") and hasattr(value, "dict"):
        return json.loads(value.json(by_alias=True))
    if hasattr(value, "dict"):
        return value.dict(by_alias=True)
    return value


def _parse_json_maybe(val: Any) -> Any:
    if isinstance(val, str):
        s = val.strip()
        if (s.startswith("{") and s.endswith("}")) or (
            s.startswith("[") and s.endswith("]")
        ):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return val
    return val


def _merge_otel_metadata(obj: dict[str, Any]) -> dict[str, Any]:
    md = _as_plain(obj.get("metadata"))
    if not isinstance(md, dict):
        return {}
    attrs = md.get("attributes")
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except json.JSONDecodeError:
            attrs = {}
    merged: dict[str, Any] = dict(attrs) if isinstance(attrs, dict) else {}
    # Langfuse v4 flattens OTel span attributes under "attributes.<key>".
    for k, v in md.items():
        if isinstance(k, str) and k.startswith("attributes."):
            merged[k[len("attributes.") :]] = v
    merged.update(md)
    return merged


def _extract_text(val: Any) -> str:
    val = _parse_json_maybe(_as_plain(val))
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        if isinstance(val.get("content"), str):
            return val["content"].strip()
        if isinstance(val.get("parts"), list):
            parts = [
                str(p["content"])
                for p in val["parts"]
                if isinstance(p, dict) and p.get("content")
            ]
            if parts:
                return "\n".join(parts).strip()
        if isinstance(val.get("messages"), list):
            return _extract_text(val["messages"])
    if isinstance(val, list):
        parts: list[str] = []
        for item in val:
            if isinstance(item, dict):
                if isinstance(item.get("parts"), list):
                    for part in item["parts"]:
                        if isinstance(part, dict) and part.get("content"):
                            parts.append(str(part["content"]))
                elif isinstance(item.get("content"), str) and item["content"].strip():
                    parts.append(item["content"])
        if parts:
            return "\n".join(parts).strip()
    return json.dumps(val, ensure_ascii=False, sort_keys=True)


def parse_utc(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    cleaned = str(ts).strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _trace_timestamp(trace: dict[str, Any]) -> datetime | None:
    raw_ts = trace.get("timestamp")
    if raw_ts:
        return parse_utc(raw_ts)
    obs_times = [
        obs.get("startTime") or obs.get("start_time")
        for obs in (trace.get("observations") or [])
        if isinstance(obs, dict) and (obs.get("startTime") or obs.get("start_time"))
    ]
    if obs_times:
        return min(parse_utc(t) for t in obs_times)
    return None


def _trace_attr(trace: dict[str, Any], *keys: str) -> str | None:
    top_md = _merge_otel_metadata(trace)
    for key in keys:
        val = trace.get(key) or top_md.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    for obs in trace.get("observations") or []:
        if not isinstance(obs, dict):
            continue
        omd = _merge_otel_metadata(obs)
        for key in keys:
            val = omd.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
    return None


def _normalize_tool_name(raw_name: str) -> str:
    name = raw_name.strip()
    if name.startswith("tool."):
        name = name[len("tool.") :]
    if name.endswith(".tool"):
        name = name[: -len(".tool")]
    return name


def _is_tool_observation(obs: dict[str, Any], omd: dict[str, Any]) -> bool:
    obs_type = str(obs.get("type") or "").upper()
    name = str(obs.get("name") or "")
    lowered = name.lower()
    op = str(omd.get("gen_ai.operation.name") or "").lower()
    return (
        obs_type == "TOOL"
        or op == "execute_tool"
        or name.endswith(".tool")
        or lowered.startswith("tool.")
        or bool(omd.get("gen_ai.tool.name"))
    )


def _extract_trace_models(trace: dict[str, Any]) -> list[str]:
    models: list[str] = []
    seen: set[str] = set()
    for obs in trace.get("observations") or []:
        if not isinstance(obs, dict):
            continue
        omd = _merge_otel_metadata(obs)
        for candidate in (
            obs.get("model"),
            obs.get("model_id"),
            omd.get("gen_ai.request.model"),
            omd.get("gen_ai.response.model"),
        ):
            if candidate is not None and str(candidate).strip():
                m = str(candidate).strip()
                if m not in seen:
                    seen.add(m)
                    models.append(m)
    return models


def _canonical_model_name(model: str) -> str:
    cleaned = model.strip().lower()
    if "/" in cleaned:
        cleaned = cleaned.split("/", 1)[1]
    return cleaned


def models_match(observed: str, configured: str) -> bool:
    obs_clean = observed.strip().lower()
    cfg_clean = configured.strip().lower()
    if obs_clean == cfg_clean:
        return True
    return _canonical_model_name(obs_clean) == _canonical_model_name(cfg_clean)


def _extract_turn_io(trace: dict[str, Any]) -> tuple[str, str]:
    user_text = _extract_text(trace.get("input"))
    assistant_text = _extract_text(trace.get("output"))

    if not user_text or not assistant_text:
        for obs in trace.get("observations") or []:
            if not isinstance(obs, dict):
                continue
            omd = _merge_otel_metadata(obs)
            if not user_text and omd.get("gen_ai.input.messages"):
                user_text = _extract_text(omd.get("gen_ai.input.messages"))
            if not assistant_text and omd.get("gen_ai.output.messages"):
                assistant_text = _extract_text(omd.get("gen_ai.output.messages"))
    return user_text, assistant_text


def _extract_turn_messages_and_tools(
    trace: dict[str, Any],
) -> tuple[list[dict[str, Any]], set[str], bool]:
    """Return HW5-identical messages, observed tool names, and whether turn completed."""
    messages: list[dict[str, Any]] = []
    tools: set[str] = set()

    user_text, assistant_text = _extract_turn_io(trace)
    if user_text:
        messages.append({"role": "user", "text": user_text})

    obs_list = sorted(
        [o for o in (trace.get("observations") or []) if isinstance(o, dict)],
        key=lambda o: str(o.get("startTime") or o.get("start_time") or ""),
    )
    for obs in obs_list:
        omd = _merge_otel_metadata(obs)
        if not _is_tool_observation(obs, omd):
            continue
        raw_name = str(omd.get("gen_ai.tool.name") or obs.get("name") or "tool")
        t_name = _normalize_tool_name(raw_name)
        if t_name:
            tools.add(t_name)

        raw_args = obs.get("input")
        if raw_args is None:
            raw_args = omd.get("gen_ai.tool.call.arguments")
        if raw_args is None:
            raw_args = omd.get("traceloop.entity.input")

        raw_res = obs.get("output")
        if raw_res is None:
            raw_res = omd.get("gen_ai.tool.call.result")
        if raw_res is None:
            raw_res = omd.get("traceloop.entity.output")

        parsed_args = _parse_json_maybe(_as_plain(raw_args))
        parsed_res = _parse_json_maybe(_as_plain(raw_res))

        if isinstance(parsed_args, dict):
            call_payload: dict[str, Any] = {"tool": t_name, **parsed_args}
        else:
            call_payload = {"tool": t_name, "arguments": parsed_args}

        messages.append(
            {
                "role": "tool_call",
                "name": t_name,
                "arguments": call_payload,
            }
        )
        if parsed_res is not None:
            messages.append(
                {
                    "role": "tool_result",
                    "name": t_name,
                    "content": {
                        "tool": t_name,
                        "result": parsed_res,
                    },
                }
            )

    if assistant_text:
        messages.append({"role": "assistant", "text": assistant_text})

    return messages, tools, bool(assistant_text)


# ---------------------------------------------------------------------------
# Langfuse fetching and conversation building
# ---------------------------------------------------------------------------


def _group_v2_observations_into_traces(
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reassemble Langfuse v4 /api/public/v2/observations rows by traceId."""
    by_trace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        tid = str(obs.get("traceId") or obs.get("trace_id") or "")
        if tid:
            by_trace[tid].append(obs)

    traces: list[dict[str, Any]] = []
    for tid, obs_list in by_trace.items():
        obs_list.sort(key=lambda o: str(o.get("startTime") or o.get("start_time") or ""))
        root = next(
            (
                o
                for o in obs_list
                if o.get("name") == "cartwheel.session_message"
            ),
            None,
        )
        if root is None:
            root = next(
                (o for o in obs_list if not o.get("parentObservationId")),
                obs_list[0],
            )
        traces.append(
            {
                "id": tid,
                "timestamp": root.get("startTime") or obs_list[0].get("startTime"),
                "input": root.get("input"),
                "output": root.get("output"),
                "metadata": root.get("metadata") or {},
                "observations": obs_list,
            }
        )
    return traces


def _fetch_v2_observations(
    from_dt: datetime,
    to_dt: datetime,
    *,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    """Fetch observations from Langfuse Cloud v4 /api/public/v2/observations."""
    host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not host or not pk or not sk:
        raise RuntimeError(
            "Langfuse credentials missing (LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY)"
        )
    auth = base64.b64encode(f"{pk}:{sk}".encode("utf-8")).decode("ascii")
    from_iso = from_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    to_iso = to_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    collected: list[dict[str, Any]] = []
    seen_obs_ids: set[str] = set()
    cursor: str | None = None
    while True:
        query_params: dict[str, Any] = {
            "fields": "core,basic,io,metadata,model,usage",
            "fromStartTime": from_iso,
            "toStartTime": to_iso,
            "limit": page_size,
        }
        if cursor:
            query_params["cursor"] = cursor
        url = f"{host}/api/public/v2/observations?{urllib.parse.urlencode(query_params)}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Basic {auth}"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        batch = list(payload.get("data") or [])
        for obs in batch:
            oid = str(obs.get("id") or "")
            if oid and oid in seen_obs_ids:
                continue
            if oid:
                seen_obs_ids.add(oid)
            collected.append(obs)
        meta = payload.get("meta") or {}
        next_cursor = meta.get("cursor")
        if not next_cursor or not batch:
            break
        cursor = str(next_cursor)

    return _group_v2_observations_into_traces(collected)


def fetch_window_traces(
    from_dt: datetime,
    to_dt: datetime,
    *,
    client: Any | None = None,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    """Fetch full Langfuse traces inside [from_dt, to_dt].

    Uses Langfuse Cloud v4's ``GET /api/public/v2/observations`` endpoint first
    (since ``GET /api/public/traces`` was removed on Langfuse Cloud on
    2026-11-19), falling back to the v3 trace API only for older self-hosted
    instances or injected test clients.
    """
    if client is None:
        try:
            traces = _fetch_v2_observations(from_dt, to_dt, page_size=page_size)
            return [
                record
                for record in traces
                if (ts := _trace_timestamp(record)) is None
                or (from_dt <= ts <= to_dt)
            ]
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise

    from analysis.helpers import langfuse_io

    lf = client or langfuse_io._client()
    summaries: list[Any] = []
    page = 1
    while True:
        resp = lf.api.trace.list(
            from_timestamp=from_dt,
            to_timestamp=to_dt,
            page=page,
            limit=page_size,
        )
        batch = list(resp.data or [])
        summaries.extend(batch)
        if len(batch) < page_size:
            break
        page += 1

    traces: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for summary in summaries:
        tid = str(getattr(summary, "id", "") or (summary.get("id") if isinstance(summary, dict) else ""))
        if not tid or tid in seen_ids:
            continue
        seen_ids.add(tid)
        full = lf.api.trace.get(tid)
        record = _as_plain(full)
        if not isinstance(record, dict):
            continue
        ts = _trace_timestamp(record)
        if ts is not None and (ts < from_dt or ts > to_dt):
            continue
        traces.append(record)
    return traces


def _validate_models_for_traces(
    traces: list[dict[str, Any]], expected_model: str
) -> None:
    for trace in traces:
        tid = str(trace.get("id") or trace.get("trace_id") or "<unknown>")
        observed = _extract_trace_models(trace)
        for model in observed:
            if not models_match(model, expected_model):
                raise ValueError(
                    f"Trace {tid} used model {model!r}, expected {expected_model!r}"
                )


def _assemble_conversation(
    ordered_turns: list[dict[str, Any]],
    *,
    scenario_id: str | None,
    session_id: str | None,
) -> dict[str, Any]:
    ordered_turns = sorted(
        ordered_turns,
        key=lambda t: (
            _trace_timestamp(t) or datetime.min.replace(tzinfo=timezone.utc),
            str(t.get("id") or ""),
        ),
    )
    all_messages: list[dict[str, Any]] = []
    all_tools: set[str] = set()
    user_turns = 0
    trace_ids: list[str] = []

    for turn in ordered_turns:
        tid = str(turn.get("id") or turn.get("trace_id") or "")
        if tid:
            trace_ids.append(tid)
        msgs, tools, _ = _extract_turn_messages_and_tools(turn)
        all_messages.extend(msgs)
        all_tools.update(tools)
        user_turns += sum(1 for m in msgs if m.get("role") == "user")

    final_trace_id = trace_ids[-1]
    record: dict[str, Any] = {
        "id": final_trace_id,
        "trace_ids": trace_ids,
        "tools": sorted(all_tools),
        "turn_count": max(user_turns, len(ordered_turns)),
        "text": _flatten(all_messages),
    }
    if scenario_id is not None:
        record["scenario_id"] = scenario_id
    if session_id is not None:
        record["session_id"] = session_id
    return record


def build_period_conversations(
    raw_traces: list[dict[str, Any]],
    expected_scenario_ids: list[str],
    expected_model: str,
    period_label: str,
) -> tuple[list[dict[str, Any]], int]:
    """Validate and group period traces by scenario_id into 50 conversations.

    Returns ``(conversations, matched_trace_count)``.
    """
    expected_set = set(expected_scenario_ids)
    matched: list[dict[str, Any]] = []
    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for trace in raw_traces:
        sid = _trace_attr(trace, "cartwheel_scenario_id", "cartwheel.scenario_id", "scenario_id")
        if sid in expected_set:
            matched.append(trace)
            by_scenario[sid].append(trace)

    missing = sorted(expected_set - set(by_scenario))
    if missing:
        preview = ", ".join(missing[:10])
        raise ValueError(
            f"Period {period_label!r} is missing {len(missing)} of "
            f"{len(expected_set)} scenario IDs ({preview})"
        )

    _validate_models_for_traces(matched, expected_model)

    conversations: list[dict[str, Any]] = []
    used_trace_count = 0
    for sid in sorted(expected_set):
        turns = by_scenario[sid]
        by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for tr in turns:
            sess = (
                _trace_attr(tr, "cartwheel.session_id", "sessionId", "session_id")
                or sid
            )
            by_session[sess].append(tr)

        completed_sessions: dict[str, list[dict[str, Any]]] = {}
        for sess, sess_turns in by_session.items():
            if any(_extract_turn_messages_and_tools(t)[2] for t in sess_turns):
                completed_sessions[sess] = sess_turns

        if not completed_sessions:
            raise ValueError(
                f"Period {period_label!r} scenario {sid!r} has no completed traces"
            )
        if len(completed_sessions) > 1:
            raise ValueError(
                f"Period {period_label!r} scenario {sid!r} contains "
                f"{len(completed_sessions)} distinct completed sessions "
                f"({sorted(completed_sessions)}); do not combine separate retries"
            )

        session_id, selected_turns = next(iter(completed_sessions.items()))
        used_trace_count += len(selected_turns)
        conversations.append(
            _assemble_conversation(
                selected_turns, scenario_id=sid, session_id=session_id
            )
        )

    if len(conversations) != len(expected_set):
        raise ValueError(
            f"Expected {len(expected_set)} conversations, built {len(conversations)}"
        )

    return conversations, used_trace_count


def build_session_conversations(
    raw_traces: list[dict[str, Any]],
    expected_model: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Group window traces by meta.session_id into conversations (--last-hours mode)."""
    eligible: list[dict[str, Any]] = []
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for trace in raw_traces:
        sess = _trace_attr(trace, "cartwheel.session_id", "sessionId", "session_id")
        if not sess:
            continue
        eligible.append(trace)
        by_session[sess].append(trace)

    if not eligible:
        return [], 0

    if expected_model:
        _validate_models_for_traces(eligible, expected_model)

    conversations: list[dict[str, Any]] = []
    used_trace_count = 0
    for sess in sorted(
        by_session,
        key=lambda s: (
            min(
                (_trace_timestamp(t) or datetime.min.replace(tzinfo=timezone.utc))
                for t in by_session[s]
            ),
            s,
        ),
    ):
        turns = by_session[sess]
        if not any(_extract_turn_messages_and_tools(t)[2] for t in turns):
            continue
        sid = _trace_attr(turns[-1], "cartwheel_scenario_id", "cartwheel.scenario_id", "scenario_id")
        used_trace_count += len(turns)
        conversations.append(
            _assemble_conversation(turns, scenario_id=sid, session_id=sess)
        )

    return conversations, used_trace_count


# ---------------------------------------------------------------------------
# Judging, persistence, and orchestration
# ---------------------------------------------------------------------------


def _is_transient_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in TRANSIENT_ERROR_MARKERS)


def _judge_with_transport_retry(
    judge_id: str,
    traces: list[dict[str, Any]],
    *,
    judge_fn: Callable[[str, list[dict[str, Any]]], dict[str, int]] = judge_sample,
) -> dict[str, int]:
    last_exc: Exception | None = None
    for attempt in range(len(RETRY_BACKOFF_SECONDS) + 1):
        try:
            return judge_fn(judge_id, traces)
        except ValueError:
            raise
        except Exception as exc:  # pragma: no cover - transient API retry
            last_exc = exc
            if attempt >= len(RETRY_BACKOFF_SECONDS) or not _is_transient_error(exc):
                raise
            wait_s = RETRY_BACKOFF_SECONDS[attempt]
            print(
                f"Transient judge transport error ({type(exc).__name__}: {exc}); "
                f"retrying in {wait_s}s...",
                file=sys.stderr,
            )
            time.sleep(wait_s)
    assert last_exc is not None
    raise last_exc


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    required = {"judge_id", "judge_mode", "model", "random_rate", "risk_groups", "threshold"}
    missing = required - set(config)
    if missing:
        raise ValueError(f"monitoring config missing required keys: {sorted(missing)}")
    return config


def load_monitoring_scenario_ids(path: Path) -> list[str]:
    ids = [
        json.loads(line)["id"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(ids) != 50 or len(set(ids)) != 50:
        raise ValueError(f"Expected 50 unique monitoring scenario IDs, got {len(ids)}")
    return ids


def _resolve_risk_groups(names: list[str]) -> dict[str, Callable[[dict[str, Any]], bool]]:
    if not names:
        raise ValueError("config.risk_groups must contain at least one risk group")
    resolved: dict[str, Callable[[dict[str, Any]], bool]] = {}
    for name in names:
        if name not in DEFAULT_RISK_GROUPS:
            raise ValueError(
                f"Unknown risk group {name!r}; expected one of {sorted(DEFAULT_RISK_GROUPS)}"
            )
        resolved[name] = DEFAULT_RISK_GROUPS[name]
    return resolved


def _upsert_history(
    history_path: Path, record: dict[str, Any], period_order: list[str]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if history_path.exists():
        for line in history_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    by_period = {row["period"]: row for row in rows}
    by_period[record["period"]] = record

    ordered: list[dict[str, Any]] = []
    for label in period_order:
        if label in by_period:
            ordered.append(by_period.pop(label))
    ordered.extend(by_period.values())

    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in ordered) + "\n",
        encoding="utf-8",
    )
    return ordered


def run_monitoring(
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    scenarios_path: Path = DEFAULT_SCENARIOS_PATH,
    history_path: Path = DEFAULT_HISTORY_PATH,
    chart_path: Path = DEFAULT_CHART_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    period: str | None = None,
    last_hours: float | None = None,
    seed: int = 7,
    dry_run: bool = False,
    skip_score_write: bool = False,
    fetch_fn: Callable[[datetime, datetime], list[dict[str, Any]]] | None = None,
    judge_fn: Callable[[str, list[dict[str, Any]]], dict[str, int]] = judge_sample,
    post_scores_fn: Callable[[list[dict[str, Any]]], int] = post_scores,
) -> dict[str, Any]:
    if bool(period) == (last_hours is not None):
        raise ValueError("Pass exactly one of --period or --last-hours")

    load_env()
    config = load_config(config_path)
    judge_id = str(config["judge_id"])
    mode = str(config["judge_mode"])
    expected_model = str(config["model"])
    random_rate = float(config["random_rate"])
    threshold = float(config["threshold"])
    risk_group_map = _resolve_risk_groups(list(config["risk_groups"]))

    judge = load_monitoring_judge(judge_id)
    if judge.get("mode") != mode:
        raise ValueError(
            f"Config judge_mode {mode!r} does not match frozen judge mode {judge.get('mode')!r}"
        )

    fetcher = fetch_fn or (lambda start, end: fetch_window_traces(start, end))

    if period is not None:
        periods = config.get("periods") or []
        period_cfg = next((p for p in periods if p.get("label") == period), None)
        if period_cfg is None:
            raise ValueError(f"Period {period!r} not found in {config_path}")
        from_dt = parse_utc(period_cfg["from"])
        to_dt = parse_utc(period_cfg["to"])
        batch_label = period
        raw_traces = fetcher(from_dt, to_dt)
        scenario_ids = load_monitoring_scenario_ids(scenarios_path)
        conversations, trace_count = build_period_conversations(
            raw_traces, scenario_ids, expected_model, period
        )
    else:
        assert last_hours is not None
        if last_hours <= 0:
            raise ValueError("--last-hours must be positive")
        to_dt = datetime.now(timezone.utc)
        from_dt = to_dt - timedelta(hours=last_hours)
        batch_label = f"last-{int(last_hours) if float(last_hours).is_integer() else last_hours}h-{to_dt.strftime('%Y-%m-%d')}"
        raw_traces = fetcher(from_dt, to_dt)
        conversations, trace_count = build_session_conversations(
            raw_traces, expected_model=expected_model
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    if not conversations:
        empty_summary = {
            "status": "no_conversations",
            "batch_label": batch_label,
            "from": from_dt.isoformat(),
            "to": to_dt.isoformat(),
            "judge_id": judge_id,
            "judge_mode": mode,
            "model": expected_model,
            "trace_count": 0,
            "conversation_count": 0,
            "random_count": 0,
            "risk_count": 0,
            "judge_calls": 0,
        }
        (output_dir / "last_run.json").write_text(
            json.dumps(empty_summary, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"[{batch_label}] 0 eligible conversations in window "
            f"({from_dt.isoformat()} to {to_dt.isoformat()}); skipping judge calls."
        )
        return empty_summary

    plan = select_traces(
        conversations,
        random_rate=random_rate,
        risk_groups=risk_group_map,
        seed=seed,
    )
    random_ids = [t["id"] for t in plan["random"]]
    risk_unique_ids: list[str] = []
    seen_risk: set[str] = set()
    for group_traces in plan["risk_groups"].values():
        for t in group_traces:
            if t["id"] not in seen_risk:
                seen_risk.add(t["id"])
                risk_unique_ids.append(t["id"])
    overlap_ids = [tid for tid in random_ids if tid in seen_risk]
    risk_group_counts = {
        name: len(items) for name, items in plan["risk_groups"].items()
    }

    print(
        f"Monitoring plan [{batch_label}]: "
        f"model={expected_model}, judge={judge_id} ({judge['model']})"
    )
    print(
        f"  Langfuse traces: {trace_count} | Conversations: {len(conversations)} | "
        f"Random sample: {len(random_ids)} | Risk unique: {len(risk_unique_ids)} "
        f"({risk_group_counts}) | Overlap: {len(overlap_ids)}"
    )
    print(f"  Total unique traces selected / judge calls: {len(plan['to_judge'])}")

    if dry_run:
        return {
            "status": "dry_run",
            "batch_label": batch_label,
            "trace_count": trace_count,
            "conversation_count": len(conversations),
            "random_count": len(random_ids),
            "risk_count": len(risk_unique_ids),
            "risk_group_counts": risk_group_counts,
            "overlap_count": len(overlap_ids),
            "judge_calls": len(plan["to_judge"]),
        }

    all_verdicts = _judge_with_transport_retry(
        judge_id, plan["to_judge"], judge_fn=judge_fn
    )

    random_verdicts = {tid: int(all_verdicts[tid]) for tid in random_ids}
    risk_verdicts = {tid: int(all_verdicts[tid]) for tid in risk_unique_ids}

    test_labels, test_preds = judge_test_data(judge_id)
    estimate = corrected_mode_prevalence(
        list(random_verdicts.values()),
        test_labels,
        test_preds,
        seed=seed,
    )

    score_records = build_score_records(
        mode=mode,
        random_verdicts=random_verdicts,
        risk_verdicts=risk_verdicts,
        estimate=estimate,
        batch_label=batch_label,
    )
    scores_written = 0
    if not skip_score_write:
        scores_written = post_scores_fn(score_records)

    risk_flag_count = sum(risk_verdicts.values())
    risk_flag_rate = (
        round(risk_flag_count / len(risk_verdicts), 4) if risk_verdicts else 0.0
    )

    history_record = {
        "period": batch_label,
        "from": from_dt.isoformat(),
        "to": to_dt.isoformat(),
        "judge_id": judge_id,
        "judge_mode": mode,
        "judge_model": judge["model"],
        "model": expected_model,
        "trace_count": trace_count,
        "conversation_count": len(conversations),
        "random_sample_count": len(random_verdicts),
        "risk_sample_count": len(risk_verdicts),
        "risk_group_counts": risk_group_counts,
        "judge_calls": len(plan["to_judge"]),
        "raw": estimate["raw"],
        "corrected": estimate["corrected"],
        "ci_low": estimate["ci_low"],
        "ci_high": estimate["ci_high"],
        "confidence": estimate["confidence"],
        "failure_sensitivity": estimate["failure_sensitivity"],
        "pass_specificity": estimate["pass_specificity"],
        "threshold": threshold,
        "risk_flagged": risk_flag_count,
        "risk_flag_rate": risk_flag_rate,
        "scores_written": scores_written,
    }

    if period is not None:
        period_order = [p["label"] for p in (config.get("periods") or [])]
        history_rows = _upsert_history(history_path, history_record, period_order)
        chart_points = [
            {
                "label": row["period"],
                "corrected": row["corrected"],
                "ci_low": row["ci_low"],
                "ci_high": row["ci_high"],
            }
            for row in history_rows
        ]
        svg = prevalence_chart(chart_points, threshold=threshold, mode=mode)
        chart_path.parent.mkdir(parents=True, exist_ok=True)
        chart_path.write_text(svg + "\n", encoding="utf-8")

    convo_by_id = {c["id"]: c for c in conversations}
    detailed_summary = {
        **history_record,
        "status": "completed",
        "random_verdicts": random_verdicts,
        "risk_verdicts": risk_verdicts,
        "flagged_random": [
            {
                "trace_id": tid,
                "scenario_id": convo_by_id[tid].get("scenario_id"),
                "tools": convo_by_id[tid].get("tools"),
            }
            for tid, v in random_verdicts.items()
            if v == 1
        ],
        "flagged_risk": [
            {
                "trace_id": tid,
                "scenario_id": convo_by_id[tid].get("scenario_id"),
                "tools": convo_by_id[tid].get("tools"),
            }
            for tid, v in risk_verdicts.items()
            if v == 1
        ],
        "score_ids": [r["score_id"] for r in score_records],
    }
    summary_file = (
        output_dir / f"period-{period}.json"
        if period is not None
        else output_dir / "last_run.json"
    )
    summary_file.write_text(
        json.dumps(detailed_summary, indent=2) + "\n", encoding="utf-8"
    )

    print(
        f"[{batch_label}] raw={estimate['raw']:.4f}, "
        f"corrected={estimate['corrected']:.4f} "
        f"(95% CI {estimate['ci_low']:.4f}-{estimate['ci_high']:.4f}), "
        f"risk_flagged={risk_flag_count}/{len(risk_verdicts)}, "
        f"scores_written={scores_written}"
    )
    return detailed_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Cartwheel post-deployment monitoring (HW7)."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--period", help="Named period from monitoring/config.json")
    group.add_argument(
        "--last-hours",
        type=float,
        help="Monitor traces from the last N hours grouped by meta.session_id",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS_PATH)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY_PATH)
    parser.add_argument("--chart", type=Path, default=DEFAULT_CHART_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch, validate, and show sampling counts without calling the judge",
    )
    parser.add_argument(
        "--skip-score-write",
        action="store_true",
        help="Skip posting scores back to Langfuse",
    )
    args = parser.parse_args(argv)

    run_monitoring(
        config_path=args.config,
        scenarios_path=args.scenarios,
        history_path=args.history,
        chart_path=args.chart,
        output_dir=args.output_dir,
        period=args.period,
        last_hours=args.last_hours,
        seed=args.seed,
        dry_run=args.dry_run,
        skip_score_write=args.skip_score_write,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

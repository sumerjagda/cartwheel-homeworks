"""Session-grouped trace review server for Cartwheel Homework 4.

Adapts and extends ``analysis/server.py`` after inspecting our Module 1
Langfuse traces:

1. **Session Grouping (`cartwheel.session_id`):** Cartwheel emits one Langfuse
   trace per user turn (`310` traces across `250` scenarios). This server
   groups traces sharing `cartwheel.session_id` (or `cartwheel.scenario_id`)
   in chronological order so multi-turn follow-up replies are never separated
   from Turn 1 tool outputs.
2. **OpenTelemetry Tool Metadata Unpacking:** Our Module 1 `TOOL` spans store
   arguments and return payloads under `metadata["gen_ai.tool.call.arguments"]`,
   `metadata["gen_ai.tool.call.result"]`, and `metadata["cartwheel.permission_denied"]`
   while leaving top-level `input`/`output` as `None`. This server unpacks
   those OTel attributes into structured tool cards and policy retrieval views.
3. **Scenario Context & SPEC.md Grounding:** Joins `scenarios/support_scenarios.jsonl`
   metadata (`pool`, `extra`, `expected`) and `SPEC.md` requirement IDs.
4. **Dual Persistence (Langfuse Scores + `analysis/state/`):** Saves free-form
   open-coding notes (`annotations.json`), taxonomy (`patterns.json`), explicit
   AI suggestion Accept/Reject decisions (`suggestions.json`), and append-only
   binary mode judgments (`labels/<mode>.jsonl` + Langfuse `/api/public/scores`).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import urllib.error
import urllib.request
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ANALYSIS_DIR = HERE.parent
REPO_ROOT = ANALYSIS_DIR.parent
STATE_DIR = ANALYSIS_DIR / "state"
REPORT_DIR = ANALYSIS_DIR / "report"
UI_DIR = HERE / "ui"
TRACES_EXPORT_PATH = REPO_ROOT / "traces" / "support_traces.json"
SCENARIOS_PATH = REPO_ROOT / "scenarios" / "support_scenarios.jsonl"


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _extract_text(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        parts: list[str] = []
        for item in val:
            if isinstance(item, dict):
                for p in item.get("parts") or []:
                    if isinstance(p, dict) and p.get("content"):
                        parts.append(str(p["content"]))
        if parts:
            return "\n".join(parts)
    return json.dumps(val, ensure_ascii=False)


def _parse_json_maybe(val: Any) -> Any:
    if isinstance(val, str):
        s = val.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return val
    return val


def _merge_otel_metadata(obj: dict[str, Any]) -> dict[str, Any]:
    md = obj.get("metadata")
    if not isinstance(md, dict):
        return {}
    attrs = md.get("attributes")
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except json.JSONDecodeError:
            attrs = {}
    merged = dict(attrs) if isinstance(attrs, dict) else {}
    merged.update(md)
    return merged


def _fetch_raw_traces() -> tuple[list[dict[str, Any]], str]:
    """Load traces live from Langfuse when reachable, else from Module 1 export."""
    lf_host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000").rstrip("/")
    try:
        collected: list[dict[str, Any]] = []
        page = 1
        while True:
            req = urllib.request.Request(f"{lf_host}/api/public/traces?page={page}&limit=100")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            batch = payload.get("data") or []
            if not batch:
                break
            for summary in batch:
                tid = summary.get("id")
                t_req = urllib.request.Request(f"{lf_host}/api/public/traces/{tid}")
                with urllib.request.urlopen(t_req, timeout=3.0) as t_resp:
                    full = json.loads(t_resp.read().decode("utf-8"))
                md = _merge_otel_metadata(full)
                sid = full.get("cartwheel_scenario_id") or md.get("cartwheel.scenario_id")
                if sid and str(sid).startswith("support-"):
                    full["cartwheel_scenario_id"] = sid
                    collected.append(full)
            if len(batch) < 100:
                break
            page += 1
        if len(collected) >= 250:
            return collected, f"live_langfuse ({lf_host})"
    except Exception:
        pass

    raw_doc = _read_json(TRACES_EXPORT_PATH, {"traces": []})
    traces = raw_doc.get("traces", []) if isinstance(raw_doc, dict) else raw_doc
    return traces, f"export_fallback ({TRACES_EXPORT_PATH.name})"


def build_session_conversations() -> dict[str, Any]:
    """Group raw Langfuse traces by cartwheel.session_id into chronological sessions."""
    raw_traces, source_label = _fetch_raw_traces()
    scenarios_by_id: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(SCENARIOS_PATH):
        if "id" in row:
            scenarios_by_id[str(row["id"])] = row

    manifest = _read_json(STATE_DIR / "sample_manifest.json", {})
    batch_by_trace: dict[str, dict[str, str]] = {}
    for pick in manifest.get("picks", []):
        tid = pick.get("trace_id")
        if tid:
            batch_by_trace[tid] = {
                "batch": pick.get("batch", "sampled"),
                "reason": pick.get("reason", ""),
            }

    grouped_by_sess: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for tr in raw_traces:
        md = _merge_otel_metadata(tr)
        sid = tr.get("cartwheel_scenario_id") or md.get("cartwheel.scenario_id") or ""
        sess_id = tr.get("sessionId") or md.get("cartwheel.session_id") or sid or tr.get("id")
        grouped_by_sess[str(sess_id)].append(tr)

    # When a scenario had a transient retry session, retain the latest completed session per scenario_id
    latest_sess_for_scenario: dict[str, tuple[tuple[int, str], str, list[dict[str, Any]]]] = {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sess_id, turn_traces in grouped_by_sess.items():
        turn_traces.sort(key=lambda x: str(x.get("timestamp") or ""))
        first_md = _merge_otel_metadata(turn_traces[0])
        sid = str(turn_traces[0].get("cartwheel_scenario_id") or first_md.get("cartwheel.scenario_id") or "")
        has_output = int(any(t.get("output") is not None for t in turn_traces))
        latest_ts = str(turn_traces[-1].get("timestamp") or "")
        rank = (has_output, latest_ts)
        if sid:
            if sid not in latest_sess_for_scenario or rank > latest_sess_for_scenario[sid][0]:
                latest_sess_for_scenario[sid] = (rank, sess_id, turn_traces)
        else:
            grouped[sess_id] = turn_traces
    for _, sess_id, turn_traces in latest_sess_for_scenario.values():
        grouped[sess_id] = turn_traces

    sessions: list[dict[str, Any]] = []
    for sess_id, turn_traces in grouped.items():
        turn_traces.sort(key=lambda x: str(x.get("timestamp") or ""))
        first = turn_traces[0]
        first_md = _merge_otel_metadata(first)
        scenario_id = (
            first.get("cartwheel_scenario_id")
            or first_md.get("cartwheel.scenario_id")
            or ""
        )
        primary_trace_id = str(first.get("id"))
        scen = scenarios_by_id.get(str(scenario_id), {})
        role = first_md.get("cartwheel.user_role") or scen.get("role") or "unknown"
        user_id = first_md.get("cartwheel.user_id") or scen.get("user_id")
        store_id = first_md.get("cartwheel.store_id") or scen.get("store_id")
        prompt_version = first_md.get("cartwheel.prompt_version") or first.get("version") or ""

        turns_rendered: list[dict[str, Any]] = []
        distinct_tools: set[str] = set()
        has_permission_denial = False
        has_escalation = False
        total_tool_calls = 0

        for turn_idx, tr in enumerate(turn_traces, start=1):
            user_text = _extract_text(tr.get("input"))
            assistant_text = _extract_text(tr.get("output"))
            obs_list = sorted(
                tr.get("observations") or [],
                key=lambda o: str(o.get("startTime") or o.get("start_time") or ""),
            )
            tool_steps: list[dict[str, Any]] = []
            for obs in obs_list:
                if obs.get("type") == "TOOL" or str(obs.get("name", "")).endswith(".tool"):
                    omd = _merge_otel_metadata(obs)
                    t_name = str(omd.get("gen_ai.tool.name") or obs.get("name") or "tool").replace(".tool", "")
                    distinct_tools.add(t_name)
                    total_tool_calls += 1
                    raw_args = obs.get("input") if obs.get("input") is not None else omd.get("gen_ai.tool.call.arguments")
                    raw_res = obs.get("output") if obs.get("output") is not None else omd.get("gen_ai.tool.call.result")
                    parsed_args = _parse_json_maybe(raw_args)
                    parsed_res = _parse_json_maybe(raw_res)
                    perm_denied = bool(omd.get("cartwheel.permission_denied"))
                    if isinstance(parsed_res, dict) and parsed_res.get("error") == "permission_denied":
                        perm_denied = True
                    if perm_denied:
                        has_permission_denial = True
                    if t_name == "escalate_to_human":
                        has_escalation = True
                    is_retrieval = t_name in ("search_help_center", "get_policy")
                    tool_steps.append({
                        "span_id": obs.get("id"),
                        "tool_name": t_name,
                        "is_retrieval": is_retrieval,
                        "latency_s": obs.get("latency"),
                        "permission_denied": perm_denied,
                        "permission_denied_reason": omd.get("cartwheel.permission_denied.reason"),
                        "arguments": parsed_args,
                        "result": parsed_res,
                    })
                elif obs.get("type") == "GENERATION" and not assistant_text:
                    gen_out = _extract_text(obs.get("output"))
                    if gen_out:
                        assistant_text = gen_out

            turns_rendered.append({
                "turn_number": turn_idx,
                "trace_id": str(tr.get("id")),
                "timestamp": tr.get("timestamp"),
                "user_text": user_text,
                "tool_steps": tool_steps,
                "assistant_text": assistant_text,
            })

        sample_info = batch_by_trace.get(primary_trace_id)
        sessions.append({
            "trace_id": primary_trace_id,
            "all_turn_trace_ids": [str(t.get("id")) for t in turn_traces],
            "session_id": sess_id,
            "scenario_id": scenario_id,
            "role": role,
            "user_id": user_id,
            "store_id": store_id,
            "prompt_version": prompt_version,
            "pool": scen.get("scenario_group") or scen.get("pool") or "unknown",
            "expected": scen.get("expected") or {},
            "extra": {
                "data_quality_case": scen.get("data_quality_case_id"),
                **(scen.get("tuple") or {}),
                **(scen.get("extra") or {}),
            },
            "turn_count": len(turns_rendered),
            "tool_call_count": total_tool_calls,
            "distinct_tools": sorted(distinct_tools),
            "has_permission_denial": has_permission_denial,
            "has_escalation": has_escalation,
            "in_sample": sample_info is not None,
            "sample_batch": sample_info["batch"] if sample_info else None,
            "sample_reason": sample_info["reason"] if sample_info else None,
            "turns": turns_rendered,
        })

    sessions.sort(key=lambda s: (0 if s["in_sample"] else 1, s["scenario_id"] or s["trace_id"]))
    return {"source": source_label, "total_sessions": len(sessions), "sessions": sessions}


def _load_all_live_labels() -> dict[str, dict[str, int]]:
    """Return {mode_name: {trace_id: 0|1}} from analysis/state/labels/*.jsonl."""
    labels_dir = STATE_DIR / "labels"
    out: dict[str, dict[str, int]] = {}
    if not labels_dir.exists():
        return out
    for path in sorted(labels_dir.glob("*.jsonl")):
        mode = path.stem
        rows = _read_jsonl(path)
        live: dict[str, int] = {}
        for row in rows:
            if row.get("superseded_by"):
                continue
            if row.get("label") in (0, 1):
                live[str(row["trace_id"])] = int(row["label"])
        out[mode] = live
    return out


def _post_score_to_langfuse(trace_id: str, mode: str, label: int, comment: str = "") -> bool:
    """Write binary judgment (0=Pass, 1=Fail) to Langfuse as a NUMERIC score."""
    lf_host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000").rstrip("/")
    payload = json.dumps({
        "traceId": trace_id,
        "name": mode,
        "value": int(label),
        "dataType": "NUMERIC",
        "comment": comment or f"HW4 human binary judgment ({mode}={label})",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{lf_host}/api/public/scores",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _save_single_label(trace_id: str, mode: str, label: int, source: str = "human", comment: str = "") -> dict[str, Any]:
    """Append-only write to state/labels/<mode>.jsonl (marking prior rows superseded_by) + Langfuse sync."""
    path = STATE_DIR / "labels" / f"{mode}.jsonl"
    rows = _read_jsonl(path)
    existing_for_trace = [r for r in rows if str(r.get("trace_id")) == trace_id]
    new_idx = len(existing_for_trace)
    new_label_id = f"{trace_id}#{new_idx}"
    updated_rows: list[dict[str, Any]] = []
    for r in rows:
        if str(r.get("trace_id")) == trace_id and not r.get("superseded_by"):
            r = {**r, "superseded_by": new_label_id}
        updated_rows.append(r)
    updated_rows.append({
        "trace_id": trace_id,
        "label": int(label),
        "source": source,
        "ts": _utcnow(),
        "label_id": new_label_id,
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in updated_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)

    lf_synced = _post_score_to_langfuse(trace_id, mode, int(label), comment=comment)
    return {"ok": True, "label_id": new_label_id, "langfuse_synced": lf_synced}


class CustomReviewHandler(BaseHTTPRequestHandler):
    """HTTP handler for the Cartwheel Homework 4 Custom Review Interface."""

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return None
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json({}, status=204)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_html(UI_DIR / "index.html")
            return
        if path == "/api/bootstrap":
            sessions_payload = build_session_conversations()
            annotations_doc = _read_json(STATE_DIR / "annotations.json", {"annotations": []})
            patterns_doc = _read_json(STATE_DIR / "patterns.json", {"modes": []})
            suggestions_doc = _read_json(STATE_DIR / "suggestions.json", [])
            manifest_doc = _read_json(STATE_DIR / "sample_manifest.json", {})
            labels_map = _load_all_live_labels()
            reports_map = {}
            for rname in ("interface_comparison.md", "review_summary.md"):
                rp = REPORT_DIR / rname
                if rp.exists():
                    reports_map[rname] = rp.read_text(encoding="utf-8")
            spec_text = (REPO_ROOT / "SPEC.md").read_text(encoding="utf-8") if (REPO_ROOT / "SPEC.md").exists() else ""
            self._send_json({
                **sessions_payload,
                "annotations": annotations_doc.get("annotations", []) if isinstance(annotations_doc, dict) else annotations_doc,
                "modes": [m for m in patterns_doc.get("modes", []) if m.get("name") != "unsupported_policy_claim"],
                "all_modes_raw": patterns_doc.get("modes", []),
                "suggestions": suggestions_doc,
                "sample_manifest": manifest_doc,
                "labels": labels_map,
                "reports": reports_map,
                "spec_md": spec_text,
            })
            return
        if path == "/api/annotations":
            self._send_json(_read_json(STATE_DIR / "annotations.json", {"annotations": []}))
            return
        if path == "/api/patterns":
            self._send_json(_read_json(STATE_DIR / "patterns.json", {"modes": []}))
            return
        if path == "/api/suggestions":
            self._send_json(_read_json(STATE_DIR / "suggestions.json", []))
            return
        if path == "/api/labels":
            self._send_json(_load_all_live_labels())
            return
        self._send_json({"error": f"not found: {path}"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = self._read_body()
        if body is None:
            self._send_json({"error": "invalid JSON body"}, status=400)
            return

        if path == "/api/annotate":
            trace_id = str(body["trace_id"])
            note = str(body.get("note", "")).strip()
            scenario_id = str(body.get("scenario_id", ""))
            doc = _read_json(STATE_DIR / "annotations.json", {"annotations": []})
            anns = doc.get("annotations", []) if isinstance(doc, dict) else list(doc)
            updated = False
            for idx, a in enumerate(anns):
                if a.get("trace_id") == trace_id and a.get("author") == "human":
                    anns[idx] = {
                        **a,
                        "note": note,
                        "scenario_id": scenario_id or a.get("scenario_id", ""),
                        "no_failure": note.lower() == "no failure observed",
                        "ts": _utcnow(),
                    }
                    updated = True
                    break
            if not updated:
                anns.append({
                    "id": f"ann-{len(anns) + 1:03d}",
                    "trace_id": trace_id,
                    "scenario_id": scenario_id,
                    "note": note,
                    "no_failure": note.lower() == "no failure observed",
                    "author": "human",
                    "ts": _utcnow(),
                })
            _write_json(STATE_DIR / "annotations.json", {"annotations": anns})
            self._send_json({"ok": True, "count": len(anns)})
            return

        if path == "/api/label":
            res = _save_single_label(
                trace_id=str(body["trace_id"]),
                mode=str(body["mode"]),
                label=int(body["label"]),
                source=str(body.get("source", "human")),
                comment=str(body.get("comment", "")),
            )
            self._send_json(res)
            return

        if path == "/api/suggestion-decision":
            sug_id = str(body["id"])
            decision = str(body["decision"])  # "accepted" or "rejected"
            reason = str(body.get("reason", ""))
            suggestions = _read_json(STATE_DIR / "suggestions.json", [])
            target = None
            for s in suggestions:
                if s.get("id") == sug_id:
                    s["status"] = decision
                    s["decision_ts"] = _utcnow()
                    if reason:
                        s["decision_reason"] = reason
                    target = s
                    break
            _write_json(STATE_DIR / "suggestions.json", suggestions)
            if target and decision == "accepted":
                _save_single_label(
                    trace_id=str(target["trace_id"]),
                    mode=str(target["mode"]),
                    label=1,
                    source="accepted_suggestion",
                    comment=str(target.get("text", "")),
                )
            self._send_json({"ok": True, "suggestion": target})
            return

        self._send_json({"error": f"unknown POST endpoint: {path}"}, status=404)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8020)
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), CustomReviewHandler)
    print(f"Cartwheel HW4 Review App listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()

"""Homework 5: Prepare judge inputs, split labels, and run/evaluate LLM judges.

Implements the four workflow entry points specified in ``homework/module-2/hw5.md``:
  - ``export_hw5_labels(mode)``: Export HW4 mode labels to ``analysis/state/hw5_labels/<mode>.jsonl``
    using the HW5 convention (``1 = Pass``, ``0 = Fail``) while keeping HW4 labels intact.
  - ``prepare_inputs(mode)``: Reconstruct one clean multi-turn conversation record per
    labelled session (unpacking OTel tool calls, tool results, and retrieved policy
    passages while stripping all human notes, labels, and scenario ``extra`` metadata)
    into ``analysis/state/hw5_trace_inputs.json``.
  - ``split_data(mode)``: Stratify-split eligible labels into 20% train / 40% dev / 40% test
    via ``analysis.helpers.split_labels`` with ``seed=7`` and ``min_per_class=10``.
  - ``run_development(mode, prompt_path, judge_model="gpt-4o-mini")``: Register a judge
    prompt version, run it on ``dev`` via DocETL, and save metrics to
    ``analysis/report/dev-<judge_id>.json``.
  - ``run_test(judge_id)``: Freeze the chosen judge version, evaluate held-out ``test``
    traces once, and save metrics to ``analysis/report/test-<judge_id>.json``.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from analysis.helpers import (
    freeze_judge,
    judge_alignment,
    register_judge,
    run_judge,
    split_labels,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")
STATE_DIR = REPO_ROOT / "analysis" / "state"
REPORT_DIR = REPO_ROOT / "analysis" / "report"
TRACES_EXPORT_PATH = REPO_ROOT / "traces" / "support_traces.json"

DEFAULT_MODE = "raw_schema_or_planning_leak"


def _extract_text(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        parts: list[str] = []
        for item in val:
            if isinstance(item, dict):
                for part in item.get("parts") or []:
                    if isinstance(part, dict) and part.get("content"):
                        parts.append(str(part["content"]))
        if parts:
            return "\n".join(parts)
    return json.dumps(val, ensure_ascii=False, sort_keys=True)


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


def export_hw5_labels(mode: str = DEFAULT_MODE) -> Path:
    """Export HW4 labels (1=Fail, 0=Pass) to HW5 convention (1=Pass, 0=Fail).

    Preserves the original HW4 file under ``analysis/state/labels/<mode>.jsonl``
    and writes ``analysis/state/hw5_labels/<mode>.jsonl``.
    """
    src_path = STATE_DIR / "labels" / f"{mode}.jsonl"
    if not src_path.exists():
        raise FileNotFoundError(f"Missing HW4 label file: {src_path}")

    dst_dir = STATE_DIR / "hw5_labels"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_path = dst_dir / f"{mode}.jsonl"

    rows = [
        json.loads(line)
        for line in src_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    out_lines: list[str] = []
    for row in rows:
        if row.get("superseded_by"):
            continue
        hw4_label = int(row["label"])
        hw5_row = {
            **row,
            "label": 1 - hw4_label,
            "verdict": "Pass" if hw4_label == 0 else "Fail",
            "label_convention": "pass_positive",
        }
        out_lines.append(json.dumps(hw5_row, ensure_ascii=False))

    dst_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return dst_path


def _load_raw_traces() -> list[dict[str, Any]]:
    raw_doc = json.loads(TRACES_EXPORT_PATH.read_text(encoding="utf-8"))
    if isinstance(raw_doc, dict):
        return list(raw_doc.get("traces", []))
    return list(raw_doc)


def prepare_inputs(mode: str = DEFAULT_MODE) -> list[dict[str, Any]]:
    """Build clean per-conversation judge inputs and save ``hw5_trace_inputs.json``.

    Includes for each labelled conversation:
      - ``trace_id``: primary trace ID for the conversation
      - ``trace``: chronological list of ``user``, ``tool_call``, ``tool_result``,
        and ``assistant`` messages across all turns of the conversation

    Strips all human review notes, failure annotations, labels, and scenario
    ``extra``/``expected`` metadata so nothing leaks the answer to the judge.
    """
    hw5_label_path = STATE_DIR / "hw5_labels" / f"{mode}.jsonl"
    if not hw5_label_path.exists():
        export_hw5_labels(mode)

    label_rows = [
        json.loads(line)
        for line in hw5_label_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    eligible_ids = [
        str(row["trace_id"]) for row in label_rows if not row.get("superseded_by")
    ]
    eligible_set = set(eligible_ids)

    raw_traces = _load_raw_traces()
    grouped_by_sess: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for tr in raw_traces:
        md = _merge_otel_metadata(tr)
        sid = tr.get("cartwheel_scenario_id") or md.get("cartwheel.scenario_id") or ""
        sess_id = (
            tr.get("sessionId")
            or md.get("cartwheel.session_id")
            or sid
            or tr.get("id")
        )
        grouped_by_sess[str(sess_id)].append(tr)

    by_primary_trace_id: dict[str, list[dict[str, Any]]] = {}
    for sess_id, turn_traces in grouped_by_sess.items():
        turn_traces.sort(key=lambda x: str(x.get("timestamp") or ""))
        for tr in turn_traces:
            tid = str(tr.get("id") or "")
            if tid in eligible_set:
                by_primary_trace_id[tid] = turn_traces

    records: list[dict[str, Any]] = []
    for tid in eligible_ids:
        turn_traces = by_primary_trace_id.get(tid)
        if not turn_traces:
            raise ValueError(f"Labeled trace_id {tid} not found in trace export")

        messages: list[dict[str, Any]] = []
        for tr in turn_traces:
            user_text = _extract_text(tr.get("input"))
            if user_text:
                messages.append({"role": "user", "text": user_text})

            obs_list = sorted(
                tr.get("observations") or [],
                key=lambda o: str(o.get("startTime") or o.get("start_time") or ""),
            )
            for obs in obs_list:
                if obs.get("type") == "TOOL" or str(obs.get("name", "")).endswith(
                    ".tool"
                ):
                    omd = _merge_otel_metadata(obs)
                    t_name = str(
                        omd.get("gen_ai.tool.name") or obs.get("name") or "tool"
                    ).replace(".tool", "")
                    raw_args = (
                        obs.get("input")
                        if obs.get("input") is not None
                        else omd.get("gen_ai.tool.call.arguments")
                    )
                    raw_res = (
                        obs.get("output")
                        if obs.get("output") is not None
                        else omd.get("gen_ai.tool.call.result")
                    )
                    parsed_args = _parse_json_maybe(raw_args)
                    parsed_res = _parse_json_maybe(raw_res)

                    if isinstance(parsed_args, dict):
                        call_payload: dict[str, Any] = {
                            "tool": t_name,
                            **parsed_args,
                        }
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

            assistant_text = _extract_text(tr.get("output"))
            if assistant_text:
                messages.append({"role": "assistant", "text": assistant_text})

        records.append(
            {
                "trace_id": tid,
                "trace": messages,
            }
        )

    out_path = STATE_DIR / "hw5_trace_inputs.json"
    out_path.write_text(
        json.dumps(records, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.environ["CARTWHEEL_JUDGE_TRACE_SOURCE"] = str(out_path.resolve())
    return records


def split_data(mode: str = DEFAULT_MODE) -> dict[str, list[str]]:
    """Split eligible labels into 20% train / 40% dev / 40% test (seed=7)."""
    inputs_path = STATE_DIR / "hw5_trace_inputs.json"
    if not inputs_path.exists():
        prepare_inputs(mode)
    records = json.loads(inputs_path.read_text(encoding="utf-8"))
    splits = split_labels(
        mode,
        fractions=(0.20, 0.40, 0.40),
        seed=7,
        min_per_class=10,
        eligible_trace_ids=[record["trace_id"] for record in records],
    )
    return splits


def run_development(
    mode: str,
    prompt_path: str | Path,
    judge_model: str | None = None,
    batch_size: int = 10,
) -> dict[str, Any]:
    """Register a judge prompt version, score the dev split, and save report."""
    inputs_path = STATE_DIR / "hw5_trace_inputs.json"
    os.environ["CARTWHEEL_JUDGE_TRACE_SOURCE"] = str(inputs_path.resolve())
    if judge_model is None:
        judge_model = (
            "gpt-4o-mini"
            if os.environ.get("OPENAI_API_KEY")
            else "gemini/gemini-3.1-flash-lite"
        )

    prompt_text = Path(prompt_path).read_text(encoding="utf-8")
    record = register_judge(
        mode=mode,
        prompt_text=prompt_text,
        judge_model=judge_model,
    )
    judge_id = record["judge_id"]
    run_judge(judge_id, split="dev", batch_size=batch_size)
    development = judge_alignment(judge_id, split="dev")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"dev-{judge_id}.json"
    report_path.write_text(
        json.dumps(development, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return development


def run_test(
    judge_id: str,
    batch_size: int = 10,
) -> dict[str, Any]:
    """Freeze the chosen judge version, score the test split once, and save report."""
    inputs_path = STATE_DIR / "hw5_trace_inputs.json"
    os.environ["CARTWHEEL_JUDGE_TRACE_SOURCE"] = str(inputs_path.resolve())

    freeze_judge(judge_id)
    run_judge(judge_id, split="test", batch_size=batch_size)
    test = judge_alignment(judge_id, split="test")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"test-{judge_id}.json"
    report_path.write_text(
        json.dumps(test, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return test

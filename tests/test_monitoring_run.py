"""Offline unit tests for Homework 7 monitoring pipeline."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from monitoring.run import (
    REPO_ROOT,
    _group_v2_observations_into_traces,
    build_period_conversations,
    build_session_conversations,
    load_monitoring_scenario_ids,
    models_match,
    run_monitoring,
)
from monitoring.sample import DEFAULT_RISK_GROUPS, select_traces


def _make_trace(
    trace_id: str,
    *,
    scenario_id: str | None = "support-0001",
    session_id: str = "sess-1",
    timestamp: str = "2026-10-25T18:05:20Z",
    model: str = "gemini/gemini-3.5-flash-lite",
    user_text: str = "Where is my order?",
    assistant_text: str | None = "Your order is on the way.",
    tools: list[tuple[str, dict[str, Any], dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = [
        {
            "id": f"gen-{trace_id}",
            "type": "GENERATION",
            "name": "openai.response",
            "startTime": timestamp,
            "model": model,
            "metadata": {"attributes": {"gen_ai.request.model": model}},
        }
    ]
    for idx, (tool_name, args, result) in enumerate(tools or []):
        observations.append(
            {
                "id": f"tool-{trace_id}-{idx}",
                "type": "TOOL",
                "name": f"{tool_name}.tool",
                "startTime": timestamp,
                "input": args,
                "output": result,
                "metadata": {"attributes": {"gen_ai.tool.name": tool_name}},
            }
        )
    attrs: dict[str, Any] = {"cartwheel.session_id": session_id}
    if scenario_id is not None:
        attrs["cartwheel.scenario_id"] = scenario_id
    return {
        "id": trace_id,
        "timestamp": timestamp,
        "input": [{"role": "user", "parts": [{"type": "text", "content": user_text}]}],
        "output": (
            [{"role": "assistant", "parts": [{"type": "text", "content": assistant_text}]}]
            if assistant_text is not None
            else None
        ),
        "metadata": {"attributes": attrs},
        "observations": observations,
    }


def test_models_match_handles_provider_prefix() -> None:
    assert models_match("gemini/gemini-3.5-flash-lite", "gemini/gemini-3.5-flash-lite")
    assert models_match("gemini-3.5-flash-lite", "gemini/gemini-3.5-flash-lite")
    assert not models_match("gpt-4o-mini", "gemini/gemini-3.5-flash-lite")
    assert not models_match("gemini-2.5-flash", "gemini/gemini-3.5-flash-lite")


def test_build_period_conversations_rejects_missing_scenario_and_wrong_model() -> None:
    expected = ["support-0001", "support-0002"]
    only_one = [_make_trace("a" * 32, scenario_id="support-0001")]
    with pytest.raises(ValueError, match="missing 1 of 2"):
        build_period_conversations(only_one, expected, "gemini/gemini-3.5-flash-lite", "before")

    wrong_model = [
        _make_trace("a" * 32, scenario_id="support-0001", model="gemini/gemini-3.5-flash-lite"),
        _make_trace("b" * 32, scenario_id="support-0002", model="gpt-4o-mini"),
    ]
    with pytest.raises(ValueError, match="used model 'gpt-4o-mini'"):
        build_period_conversations(wrong_model, expected, "gemini/gemini-3.5-flash-lite", "before")


def test_build_period_conversations_uses_final_trace_id_and_rejects_retry_sessions() -> None:
    expected = ["support-0001"]
    t1 = _make_trace(
        "1" * 32,
        scenario_id="support-0001",
        session_id="sess-a",
        timestamp="2026-10-25T18:05:20Z",
        user_text="First turn",
        assistant_text="Reply 1",
        tools=[("get_order", {"order_id": 1}, {"ok": True})],
    )
    t2 = _make_trace(
        "2" * 32,
        scenario_id="support-0001",
        session_id="sess-a",
        timestamp="2026-10-25T18:05:25Z",
        user_text="Second turn",
        assistant_text="Reply 2",
        tools=[("issue_refund", {"order_id": 1}, {"ok": True})],
    )
    convos, count = build_period_conversations(
        [t2, t1], expected, "gemini/gemini-3.5-flash-lite", "before"
    )
    assert count == 2
    assert len(convos) == 1
    c = convos[0]
    assert c["id"] == "2" * 32
    assert c["turn_count"] == 2
    assert c["tools"] == ["get_order", "issue_refund"]
    assert "user: First turn" in c["text"]
    assert "user: Second turn" in c["text"]
    assert "tool_call:" in c["text"]
    assert "tool_result:" in c["text"]
    assert DEFAULT_RISK_GROUPS["write_action"](c) is True
    assert DEFAULT_RISK_GROUPS["multi_turn"](c) is True

    # Two distinct completed sessions in the same period must be rejected.
    retry_session = _make_trace(
        "3" * 32,
        scenario_id="support-0001",
        session_id="sess-b",
        timestamp="2026-10-25T18:05:30Z",
    )
    with pytest.raises(ValueError, match="distinct completed sessions"):
        build_period_conversations(
            [t1, retry_session], expected, "gemini/gemini-3.5-flash-lite", "before"
        )


def test_real_hw3_monitoring_traces_build_50_conversations_cleanly() -> None:
    traces_path = REPO_ROOT / "traces" / "support_traces.json"
    scenarios_path = REPO_ROOT / "scenarios" / "monitoring_scenarios.jsonl"
    if not traces_path.exists() or not scenarios_path.exists():
        pytest.skip("HW3 trace export not present")

    expected = load_monitoring_scenario_ids(scenarios_path)
    raw_traces = json.loads(traces_path.read_text(encoding="utf-8"))["traces"]
    convos, used_count = build_period_conversations(
        raw_traces, expected, "gemini/gemini-3.5-flash-lite", "before"
    )
    assert len(convos) == 50
    assert used_count == 61
    assert len({c["id"] for c in convos}) == 50

    plan = select_traces(
        convos,
        random_rate=0.2,
        risk_groups={"write_action": DEFAULT_RISK_GROUPS["write_action"]},
        seed=7,
    )
    assert len(plan["random"]) == 10
    assert len(plan["risk_groups"]["write_action"]) == 17
    assert len(plan["to_judge"]) <= 27
    assert all("user:" in c["text"] and "assistant:" in c["text"] for c in convos)


def test_run_monitoring_last_hours_empty_exits_cleanly_without_calling_judge(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "judge_id": "raw_schema_or_planning_leak-v1",
                "judge_mode": "raw_schema_or_planning_leak",
                "model": "gemini/gemini-3.5-flash-lite",
                "random_rate": 0.2,
                "risk_groups": ["write_action"],
                "threshold": 0.15,
                "periods": [],
            }
        ),
        encoding="utf-8",
    )

    def fail_if_called(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        raise AssertionError("judge must not be called when 0 conversations exist")

    result = run_monitoring(
        config_path=cfg,
        output_dir=tmp_path / "out",
        last_hours=24,
        fetch_fn=lambda _start, _end: [],
        judge_fn=fail_if_called,
        post_scores_fn=lambda _records: 0,
    )
    assert result["status"] == "no_conversations"
    assert result["conversation_count"] == 0
    assert result["judge_calls"] == 0
    saved = json.loads((tmp_path / "out" / "last_run.json").read_text(encoding="utf-8"))
    assert saved["conversation_count"] == 0


def test_run_monitoring_period_end_to_end_and_idempotent_history(tmp_path: Path) -> None:
    scenarios_path = REPO_ROOT / "scenarios" / "monitoring_scenarios.jsonl"
    traces_path = REPO_ROOT / "traces" / "support_traces.json"
    if not traces_path.exists():
        pytest.skip("HW3 trace export not present")

    raw_traces = json.loads(traces_path.read_text(encoding="utf-8"))["traces"]
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "judge_id": "raw_schema_or_planning_leak-v1",
                "judge_mode": "raw_schema_or_planning_leak",
                "model": "gemini/gemini-3.5-flash-lite",
                "random_rate": 0.2,
                "risk_groups": ["write_action"],
                "threshold": 0.15,
                "periods": [
                    {
                        "label": "before",
                        "from": "2026-10-25T18:05:00Z",
                        "to": "2026-10-25T18:07:00Z",
                    },
                    {
                        "label": "after",
                        "from": "2026-11-25T16:00:00Z",
                        "to": "2026-11-25T17:00:00Z",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    posted_batches: list[list[dict[str, Any]]] = []

    def stub_judge(_judge_id: str, traces: list[dict[str, Any]]) -> dict[str, int]:
        return {t["id"]: (1 if i % 5 == 0 else 0) for i, t in enumerate(traces)}

    def stub_post(records: list[dict[str, Any]]) -> int:
        posted_batches.append(records)
        return len(records)

    history_path = tmp_path / "history.jsonl"
    chart_path = tmp_path / "prevalence.svg"
    out_dir = tmp_path / "out"

    first = run_monitoring(
        config_path=cfg,
        scenarios_path=scenarios_path,
        history_path=history_path,
        chart_path=chart_path,
        output_dir=out_dir,
        period="before",
        fetch_fn=lambda _start, _end: raw_traces,
        judge_fn=stub_judge,
        post_scores_fn=stub_post,
    )
    second = run_monitoring(
        config_path=cfg,
        scenarios_path=scenarios_path,
        history_path=history_path,
        chart_path=chart_path,
        output_dir=out_dir,
        period="after",
        fetch_fn=lambda _start, _end: raw_traces,
        judge_fn=stub_judge,
        post_scores_fn=stub_post,
    )
    # Re-run after to confirm idempotency of score_ids and history.jsonl rows.
    third = run_monitoring(
        config_path=cfg,
        scenarios_path=scenarios_path,
        history_path=history_path,
        chart_path=chart_path,
        output_dir=out_dir,
        period="after",
        fetch_fn=lambda _start, _end: raw_traces,
        judge_fn=stub_judge,
        post_scores_fn=stub_post,
    )

    assert first["conversation_count"] == 50
    assert second["score_ids"] == third["score_ids"]
    lines = [
        json.loads(line)
        for line in history_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [row["period"] for row in lines] == ["before", "after"]
    assert "<svg" in chart_path.read_text(encoding="utf-8")


def test_build_session_conversations_groups_by_session_id() -> None:
    t1 = _make_trace("a" * 32, scenario_id=None, session_id="live-1", timestamp="2026-11-25T15:00:00Z")
    t2 = _make_trace("b" * 32, scenario_id=None, session_id="live-1", timestamp="2026-11-25T15:01:00Z")
    t3 = _make_trace("c" * 32, scenario_id=None, session_id="live-2", timestamp="2026-11-25T15:02:00Z")
    convos, count = build_session_conversations(
        [t3, t2, t1], expected_model="gemini/gemini-3.5-flash-lite"
    )
    assert count == 3
    assert [c["session_id"] for c in convos] == ["live-1", "live-2"]
    assert convos[0]["id"] == "b" * 32
    assert convos[0]["turn_count"] == 2


def test_group_v2_observations_unpacks_langfuse_v4_flattened_attributes() -> None:
    obs = [
        {
            "id": "root-1",
            "traceId": "c" * 32,
            "parentObservationId": None,
            "type": "SPAN",
            "name": "cartwheel.session_message",
            "startTime": "2026-11-22T18:05:19.822Z",
            "input": json.dumps([{"role": "user", "parts": [{"type": "text", "content": "Check order #1"}]}]),
            "output": json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": "Order #1 is shipped."}]}]),
            "metadata": {
                "attributes.cartwheel.scenario_id": "support-0001",
                "attributes.cartwheel.session_id": "sess-v4",
            },
        },
        {
            "id": "tool-1",
            "traceId": "c" * 32,
            "parentObservationId": "root-1",
            "type": "TOOL",
            "name": "get_order",
            "startTime": "2026-11-22T18:05:20.100Z",
            "input": '{"order_id": 1}',
            "output": '{"ok": true}',
            "metadata": {"attributes.gen_ai.tool.name": "get_order"},
        },
        {
            "id": "gen-1",
            "traceId": "c" * 32,
            "parentObservationId": "root-1",
            "type": "GENERATION",
            "name": "openai.response",
            "startTime": "2026-11-22T18:05:20.200Z",
            "model": "gemini/gemini-3.5-flash-lite",
            "metadata": {},
        },
    ]
    traces = _group_v2_observations_into_traces(obs)
    convos, count = build_period_conversations(
        traces, ["support-0001"], "gemini/gemini-3.5-flash-lite", "before"
    )
    assert count == 1
    assert len(convos) == 1
    assert convos[0]["id"] == "c" * 32
    assert convos[0]["tools"] == ["get_order"]
    assert "user: Check order #1" in convos[0]["text"]
    assert "assistant: Order #1 is shipped." in convos[0]["text"]

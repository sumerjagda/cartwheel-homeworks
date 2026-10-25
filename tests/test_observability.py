"""Authentication and span instrumentation tests for Homework 2 (Part D)."""

from __future__ import annotations

import dotenv
import pytest
from fastapi import HTTPException
from opentelemetry.sdk.trace import TracerProvider

from agent.auth import AuthContext
from observability.instrument import record_tool_result
from server import app as server_app

# Prevent LiteLLM from implicitly loading .env into os.environ during offline pytest runs.
dotenv.load_dotenv = lambda *args, **kwargs: False


def test_create_session_rejects_mismatched_role(world: dict) -> None:
    """Session creation rejects a user whose claimed role differs from the DB role."""
    server_app._SESSIONS.clear()
    # User 1 is a shopper in the seeded database, not a merchant or support agent.
    with pytest.raises(HTTPException) as exc_info:
        server_app.create_session(
            server_app.SessionCreate(user_id=1, role="merchant")
        )
    assert exc_info.value.status_code == 403
    assert "shopper" in str(exc_info.value.detail)


@pytest.mark.anyio
async def test_token_for_one_session_cannot_authorize_another_session(
    world: dict,
) -> None:
    """A token issued for session A is rejected with HTTP 403 on session B."""
    server_app._SESSIONS.clear()
    session_a = server_app.create_session(
        server_app.SessionCreate(user_id=1, role="shopper")
    )
    session_b = server_app.create_session(
        server_app.SessionCreate(user_id=2, role="shopper")
    )

    with pytest.raises(HTTPException) as exc_info:
        await server_app.post_message(
            session_id=session_b["session_id"],
            body=server_app.MessageIn(message="Show my recent orders."),
            authorization=f"Bearer {session_a['token']}",
        )
    assert exc_info.value.status_code == 403
    assert "another session" in str(exc_info.value.detail)


def test_record_tool_result_sets_cartwheel_span_attributes() -> None:
    """Tool spans record caller identity and permission_denied attributes."""
    provider = TracerProvider()
    tracer = provider.get_tracer("test.hw2")

    with tracer.start_as_current_span("tool.get_order") as current_span:
        record_tool_result(
            AuthContext(user_id=9002, role="merchant", store_id=2),
            {
                "ok": False,
                "error": "permission_denied",
                "reason": "order belongs to another store",
            },
        )
        attrs = dict(current_span.attributes or {})
        assert attrs["cartwheel.user_role"] == "merchant"
        assert attrs["cartwheel.user_id"] == "9002"
        assert attrs["cartwheel.store_id"] == "2"
        assert attrs["cartwheel.permission_denied"] is True
        assert (
            attrs["cartwheel.permission_denied.reason"]
            == "order belongs to another store"
        )

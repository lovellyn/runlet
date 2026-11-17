from types import SimpleNamespace

import pytest

from examples.client import follow_job


def snapshot(state="running", output="", next_cursor=0, has_more=False, truncated=False):
    return {
        "job_id": "test-job",
        "state": state,
        "output": output,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "truncated": truncated,
    }


class Client:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        value = next(self.results)
        return SimpleNamespace(
            is_error=isinstance(value, str),
            content=value if isinstance(value, str) else [],
            structured_content=None if isinstance(value, str) else value,
        )


async def test_follow_job_waits_between_updates_and_drains_every_remaining_page():
    initial = snapshot(output="é", next_cursor=2, has_more=True)
    responses = [
        snapshot(output="b", next_cursor=3),
        snapshot(next_cursor=3),
        snapshot(state="completed", output="c", next_cursor=4, has_more=True, truncated=True),
        snapshot(state="completed", output="d", next_cursor=5),
    ]
    client = Client(responses)
    received = [value async for value in follow_job(client, initial)]
    assert received == [initial, *responses]
    assert [call[1]["cursor"] for call in client.calls] == [2, 3, 3, 4]
    assert [call[1]["wait_seconds"] for call in client.calls] == [0, 20, 20, 0]
    assert all(
        name == "job_status" and args["wait_for"] == "completion" for name, args in client.calls
    )


async def test_follow_job_forwards_explicit_output_mode():
    client = Client([snapshot(state="completed", output="done", next_cursor=4)])
    results = [
        value async for value in follow_job(client, snapshot(), wait_for="output", wait_seconds=3)
    ]
    assert results[-1]["state"] == "completed"
    assert client.calls[0][1]["wait_for"] == "output"
    assert client.calls[0][1]["wait_seconds"] == 3


async def test_follow_job_propagates_errors_without_replaying_a_command():
    client = Client(["job_not_found: server restarted"])
    with pytest.raises(RuntimeError, match="job_not_found"):
        async for _ in follow_job(client, snapshot()):
            pass
    assert len(client.calls) == 1 and client.calls[0][0] == "job_status"


async def test_follow_job_stopping_consumption_does_not_cancel_execution():
    client = Client([])
    updates = follow_job(client, snapshot())
    assert (await anext(updates))["state"] == "running"
    await updates.aclose()
    assert client.calls == []


async def test_follow_job_terminal_initial_snapshot_needs_no_request():
    initial = snapshot(state="failed", output="error", next_cursor=5)
    client = Client([])
    assert [value async for value in follow_job(client, initial)] == [initial]
    assert client.calls == []


@pytest.mark.parametrize(
    "options",
    [
        {"wait_seconds": 0},
        {"wait_seconds": -1},
        {"wait_seconds": 21},
        {"wait_seconds": float("nan")},
        {"wait_for": "invalid"},
    ],
)
async def test_follow_job_rejects_busy_polling_and_invalid_waits(options):
    client = Client([])
    with pytest.raises(ValueError):
        async for _ in follow_job(client, snapshot(), **options):
            pass
    assert client.calls == []

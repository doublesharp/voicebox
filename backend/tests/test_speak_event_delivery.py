"""Speaking events remain useful when an SSE subscriber falls behind."""

import json

import pytest

from backend.mcp_server import events


@pytest.fixture
def subscribers():
    active = []

    def subscribe():
        queue = events.subscribe()
        active.append(queue)
        return queue

    yield subscribe
    for queue in active:
        events.unsubscribe(queue)


def drain(queue):
    return [queue.get_nowait() for _ in range(queue.qsize())]


@pytest.mark.parametrize("extra", [1, 5, 64])
def test_slow_subscriber_retains_newest_events_and_terminal_state(subscribers, extra):
    queue = subscribers()
    for index in range(queue.maxsize + extra):
        events.publish("speak-start", {"generation_id": str(index)})
    events.publish("speak-end", {"generation_id": "latest", "status": "completed"})
    received = drain(queue)
    assert len(received) == queue.maxsize
    assert received[-1] == {"kind": "speak-end", "generation_id": "latest", "status": "completed"}
    assert [int(event["generation_id"]) for event in received[:-1]] == list(
        range(extra + 1, queue.maxsize + extra)
    )


def test_slow_subscriber_does_not_change_fast_subscriber_or_shared_payload(subscribers):
    slow, fast = subscribers(), subscribers()
    for index in range(slow.maxsize):
        events.publish("speak-start", {"generation_id": str(index)})
        assert fast.get_nowait()["generation_id"] == str(index)
    payload = {"generation_id": "final", "status": "completed"}
    events.publish("speak-end", payload)
    fast_event = fast.get_nowait()
    slow_event = drain(slow)[-1]
    assert fast_event == slow_event == {"kind": "speak-end", **payload}
    fast_event.pop("kind")
    assert slow_event["kind"] == "speak-end"
    assert payload == {"generation_id": "final", "status": "completed"}


def test_unsubscribed_client_does_not_receive_later_events(subscribers):
    active, closed = subscribers(), subscribers()
    events.unsubscribe(closed)
    events.publish("speak-start", {"generation_id": "new"})
    assert active.get_nowait()["generation_id"] == "new"
    assert closed.empty()


@pytest.mark.asyncio
async def test_sse_endpoint_delivers_latest_completion_after_backlog_and_unsubscribes():
    from backend.routes.events import speak_events

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    before = set(events._subscribers)
    response = await speak_events(ConnectedRequest())
    stream = response.body_iterator
    try:
        assert await anext(stream) == {"event": "ready", "data": "{}"}
        for index in range(70):
            events.publish("speak-start", {"generation_id": str(index)})
        events.publish("speak-end", {"generation_id": "final", "status": "completed"})
        received = [await anext(stream) for _ in range(64)]
        assert received[-1]["event"] == "speak-end"
        assert json.loads(received[-1]["data"]) == {"generation_id": "final", "status": "completed"}
    finally:
        await stream.aclose()
    assert events._subscribers == before

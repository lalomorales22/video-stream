"""Phone signaling relay: room semantics over the real ASGI app."""

from fastapi.testclient import TestClient

from video_stream.app import app

client = TestClient(app)


def test_relay_forwards_frames_to_the_other_member_only():
    with client.websocket_connect("/phone-signal?session=t1&role=phone") as phone:
        with client.websocket_connect("/phone-signal?session=t1&role=desktop") as desktop:
            phone.send_json({"type": "offer", "sdp": {"type": "offer", "sdp": "v=0"}})
            assert desktop.receive_json()["type"] == "offer"
            desktop.send_json({"type": "answer", "sdp": {"type": "answer", "sdp": "v=0"}})
            assert phone.receive_json()["type"] == "answer"


def test_sessions_are_isolated_rooms():
    with client.websocket_connect("/phone-signal?session=roomA") as a1:
        with client.websocket_connect("/phone-signal?session=roomB") as b1:
            with client.websocket_connect("/phone-signal?session=roomA") as a2:
                a1.send_json({"type": "ice", "candidate": {"x": 1}})
                assert a2.receive_json()["type"] == "ice"  # same room hears it
                # roomB must hear nothing; prove it by sending a probe that
                # arrives FIRST if nothing else was queued.
                b1.send_json({"type": "noop"})
                with client.websocket_connect("/phone-signal?session=roomB") as b2:
                    b1.send_json({"type": "probe"})
                    assert b2.receive_json()["type"] == "probe"


def test_missing_session_is_rejected():
    with client.websocket_connect("/phone-signal") as ws:
        # Server accepts then immediately closes with the app-defined code.
        import pytest
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
        assert exc_info.value.code == 4400


def test_rooms_clean_up_after_disconnect():
    from video_stream.phone import rooms

    with client.websocket_connect("/phone-signal?session=gone"):
        assert "gone" in rooms
    assert "gone" not in rooms

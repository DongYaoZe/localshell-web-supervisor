import unittest

from cws.cdp import (
    CdpNetworkProbe,
    CdpProbeUnavailable,
    _validate_endpoint,
    sample_cdp_session,
)


class CdpSafetyTests(unittest.TestCase):
    def test_loopback_http_is_allowed(self):
        self.assertEqual(
            _validate_endpoint("http://127.0.0.1:9222", allow_remote=False),
            "127.0.0.1",
        )
        self.assertEqual(
            _validate_endpoint("http://localhost:9222", allow_remote=False),
            "localhost",
        )

    def test_loopback_ipv6_is_allowed(self):
        self.assertEqual(
            _validate_endpoint("http://[::1]:9222", allow_remote=False),
            "::1",
        )

    def test_remote_endpoint_requires_explicit_opt_in(self):
        with self.assertRaises(CdpProbeUnavailable):
            CdpNetworkProbe("https://browser.example.test/devtools")
        probe = CdpNetworkProbe(
            "https://browser.example.test/devtools?token=do-not-persist",
            allow_remote=True,
        )
        self.assertEqual(probe._endpoint_host, "browser.example.test")

    def test_invalid_endpoint_is_rejected(self):
        with self.assertRaises(CdpProbeUnavailable):
            _validate_endpoint("not-a-url", allow_remote=False)

    def test_remote_endpoint_credentials_are_not_in_safe_metadata(self):
        probe = CdpNetworkProbe(
            "https://browser.example.test/devtools?token=do-not-persist",
            allow_remote=True,
        )
        self.assertEqual(probe._endpoint_host, "browser.example.test")


class FakeSession:
    def __init__(self):
        self.handlers = {}
        self.commands = []

    def on(self, name, handler):
        self.handlers[name] = handler

    def send(self, name):
        self.commands.append(name)

    def emit(self, name, payload):
        self.handlers[name](payload)


class CdpSamplingTests(unittest.TestCase):
    def test_sample_collects_lifecycle_metadata_only(self):
        session = FakeSession()

        def wait(_milliseconds):
            session.emit(
                "Network.requestWillBeSent",
                {
                    "requestId": "1",
                    "type": "Fetch",
                    "request": {
                        "url": "http://127.0.0.1:8765/ping",
                        "headers": {"Authorization": "must-not-be-recorded"},
                        "postData": "must-not-be-recorded",
                    },
                },
            )
            session.emit(
                "Network.responseReceived",
                {
                    "requestId": "1",
                    "response": {
                        "url": "http://127.0.0.1:8765/ping",
                        "status": 200,
                        "headers": {"Set-Cookie": "must-not-be-recorded"},
                    },
                },
            )
            session.emit(
                "Network.dataReceived",
                {"requestId": "1", "dataLength": 4, "encodedDataLength": 4},
            )
            session.emit("Network.webSocketFrameReceived", {"requestId": "ws"})
            session.emit("Network.loadingFinished", {"requestId": "1"})

        obs = sample_cdp_session(
            session,
            worker_id="w1",
            page_url="http://127.0.0.1:8765/",
            sample_s=0.01,
            source="test",
            wait=wait,
            raw_context={"ownership": "test"},
        )
        self.assertEqual(obs.request_count, 1)
        self.assertEqual(obs.response_count, 1)
        self.assertEqual(obs.data_event_count, 1)
        self.assertEqual(obs.encoded_data_bytes, 4)
        self.assertEqual(obs.loading_finished, 1)
        self.assertEqual(obs.websocket_frames, 1)
        self.assertEqual(obs.inflight_requests, 0)
        self.assertIsNotNone(obs.quiet_since_at)
        self.assertEqual(obs.quiet_since_at, obs.last_activity_at)
        self.assertEqual(obs.raw["origins"], {"127.0.0.1:8765": 2})
        rendered = repr(obs.raw)
        self.assertNotIn("Authorization", rendered)
        self.assertNotIn("must-not-be-recorded", rendered)
        self.assertNotIn("/ping", rendered)
        self.assertEqual(session.commands, ["Network.enable", "Network.disable"])

    def test_quiet_sample_carries_forward_last_activity(self):
        session = FakeSession()
        obs = sample_cdp_session(
            session,
            worker_id="w1",
            page_url="http://127.0.0.1:8765/",
            sample_s=0.01,
            source="test",
            wait=lambda _milliseconds: None,
            previous_last_activity_at=123.0,
        )
        self.assertEqual(obs.event_count, 0)
        self.assertEqual(obs.last_activity_at, 123.0)
        self.assertIsNotNone(obs.quiet_since_at)
        self.assertEqual(session.commands, ["Network.enable", "Network.disable"])

    def test_quiet_sample_preserves_existing_quiet_baseline(self):
        session = FakeSession()
        obs = sample_cdp_session(
            session,
            worker_id="w1",
            page_url="http://127.0.0.1:8765/",
            sample_s=0.01,
            source="test",
            wait=lambda _milliseconds: None,
            previous_last_activity_at=100.0,
            previous_quiet_since_at=101.0,
        )
        self.assertEqual(obs.last_activity_at, 100.0)
        self.assertEqual(obs.quiet_since_at, 101.0)


if __name__ == "__main__":
    unittest.main()

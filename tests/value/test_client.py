"""SEC politeness: a real User-Agent, a throttle, and backoff instead of hammering.

Getting this wrong costs the server's IP address, and EDGAR is the only source of
statements this module has.
"""

import unittest

import requests

from tradingagents.value.edgar.client import SecClient, SecRequestError


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}

    def json(self):
        return self._payload


class _Session:
    """Returns queued responses (or raises queued exceptions) and records headers."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.headers_seen = []

    def get(self, url, headers=None, timeout=None):
        self.headers_seen.append(headers or {})
        result = self.queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class UserAgentTest(unittest.TestCase):
    def test_a_missing_user_agent_raises_rather_than_defaulting(self):
        with self.assertRaises(ValueError):
            SecClient(user_agent="", session=_Session())

    def test_a_user_agent_without_a_contact_address_is_rejected(self):
        with self.assertRaises(ValueError):
            SecClient(user_agent="TradingAgents", session=_Session())

    def test_every_request_carries_the_contact_address(self):
        session = _Session(_Response())
        client = SecClient(user_agent="TradingAgents research me@example.com", session=session)

        client.get_json("https://data.sec.gov/whatever")

        self.assertEqual(session.headers_seen[0]["User-Agent"],
                         "TradingAgents research me@example.com")


class ThrottleTest(unittest.TestCase):
    def test_consecutive_requests_are_spaced_by_the_rate_limit(self):
        slept = []
        session = _Session(_Response(), _Response())
        client = SecClient(user_agent="a@b.com", requests_per_second=2.0,
                           session=session, sleep=slept.append)

        client.get("https://sec.gov/one")
        client.get("https://sec.gov/two")

        # First request is free; the second waits out the remaining interval.
        self.assertTrue(any(0 < wait <= 0.5 for wait in slept), slept)

    def test_an_impossible_rate_is_rejected(self):
        with self.assertRaises(ValueError):
            SecClient(user_agent="a@b.com", requests_per_second=0, session=_Session())


class RetryTest(unittest.TestCase):
    def test_rate_limiting_is_retried_with_backoff(self):
        slept = []
        session = _Session(_Response(429), _Response(200))
        client = SecClient(user_agent="a@b.com", requests_per_second=1000.0,
                           session=session, sleep=slept.append)

        response = client.get("https://sec.gov/x")

        self.assertEqual(response.status_code, 200)
        self.assertIn(1.0, slept)

    def test_a_network_error_is_retried(self):
        session = _Session(requests.ConnectionError("boom"), _Response(200))
        client = SecClient(user_agent="a@b.com", requests_per_second=1000.0,
                           session=session, sleep=lambda _: None)

        self.assertEqual(client.get("https://sec.gov/x").status_code, 200)

    def test_a_permanent_failure_is_not_retried(self):
        session = _Session(_Response(404), _Response(200))
        client = SecClient(user_agent="a@b.com", requests_per_second=1000.0,
                           session=session, sleep=lambda _: None)

        with self.assertRaises(SecRequestError):
            client.get("https://sec.gov/missing")
        self.assertEqual(len(session.queue), 1)  # the second response was never needed

    def test_exhausting_the_retries_raises_rather_than_returning_nothing(self):
        session = _Session(*[_Response(503) for _ in range(3)])
        client = SecClient(user_agent="a@b.com", requests_per_second=1000.0,
                           max_retries=3, session=session, sleep=lambda _: None)

        with self.assertRaises(SecRequestError):
            client.get("https://sec.gov/down")

    def test_non_json_response_is_an_error_not_an_empty_dict(self):
        class _Bad(_Response):
            def json(self):
                raise ValueError("not json")

        client = SecClient(user_agent="a@b.com", session=_Session(_Bad()),
                           sleep=lambda _: None)

        with self.assertRaises(SecRequestError):
            client.get_json("https://sec.gov/html")


if __name__ == "__main__":
    unittest.main()

"""Anymailfinder: the client, and which email finder a run uses — the API
stubbed, so no credits are spent."""

import pytest

from healthcare_loanscout import cli
from healthcare_loanscout.anymailfinder import AnymailfinderClient, AnymailfinderUnavailable
from healthcare_loanscout.emailfinder import EmailFinderUnavailable, FinderResult, describe


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.payload = payload or {}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def post(self, url, json, timeout):
        self.calls.append((url, json))
        return self.responses.pop(0)

    def close(self):
        pass


def answer(status="valid", email="jobrien@unityhh.com"):
    return FakeResponse(200, {"email": email, "email_status": status, "valid_email": email if status == "valid" else None,
                              "person_job_title": "CEO", "credits_charged": 1 if status == "valid" else 0})


def test_the_key_and_the_search_are_sent_as_documented():
    session = FakeSession(answer())
    AnymailfinderClient("key123", session).find("unityhh.com", "Jane", "O'Brien")
    assert session.headers["Authorization"] == "key123"
    url, body = session.calls[0]
    assert url == "https://api.anymailfinder.com/v5.1/find-email/person"
    assert body == {"domain": "unityhh.com", "first_name": "Jane", "last_name": "O'Brien"}


def test_a_valid_address_is_kept():
    finder = AnymailfinderClient("key", FakeSession(answer()))
    assert finder.find("unityhh.com", "Jane", "O'Brien") == FinderResult("jobrien@unityhh.com", "valid", 0, "CEO")
    assert (finder.lookups, finder.found) == (1, 1)


@pytest.mark.parametrize("status", ["risky", "blacklisted", "not_found"])
def test_anything_but_valid_is_not(status):
    result = AnymailfinderClient("key", FakeSession(answer(status))).find("unityhh.com", "Jane", "O'Brien")
    assert result.email == "" and result.status == status


@pytest.mark.parametrize("status,message", [(401, "rejected the API key"), (402, "out of credits")])
def test_a_refused_key_or_no_credits_stops_every_worker(status, message):
    finder = AnymailfinderClient("key", FakeSession(FakeResponse(status)))
    with pytest.raises(AnymailfinderUnavailable, match=message):
        finder.find("unityhh.com", "Jane", "O'Brien")
    with pytest.raises(EmailFinderUnavailable):
        finder.find("other.com", "Jane", "O'Brien")  # no second request


def test_a_search_it_cant_run_is_empty():
    assert AnymailfinderClient("key", FakeSession(FakeResponse(400))).find("x", "A", "B") == FinderResult()


def test_a_server_error_is_retried_next_run():
    with pytest.raises(ConnectionError):
        AnymailfinderClient("key", FakeSession(FakeResponse(503))).find("unityhh.com", "Jane", "O'Brien")


def test_the_source_names_the_service():
    assert describe(AnymailfinderClient("key", FakeSession()), FinderResult("a@b.com", "valid")) == \
        "Anymailfinder (verified)"


class Keys:
    def __init__(self, anymailfinder=None):
        self.anymailfinder_api_key = anymailfinder


def test_anymailfinder_is_used_when_its_key_is_set():
    assert isinstance(cli.make_email_finder(Keys("a")), AnymailfinderClient)
    assert cli.make_email_finder(Keys()) is None

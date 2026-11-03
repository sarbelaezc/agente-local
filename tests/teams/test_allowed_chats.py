from automation.teams_web import TeamsSafetyError, TeamsWebAutomation
from config.settings import AllowedChat


class FakeContext:
    pages = []


def test_unknown_chat_is_rejected():
    teams = TeamsWebAutomation(
        FakeContext(),
        "https://teams.microsoft.com/",
        (AllowedChat(id="allowed", name="Allowed"),),
    )
    try:
        teams.open_chat("unknown")
    except TeamsSafetyError:
        return
    raise AssertionError("Unknown chat was accepted")

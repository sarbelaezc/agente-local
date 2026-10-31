from automation.teams_web import TeamsSafetyError, TeamsWebAutomation
from config.settings import AllowedChat


class FakeContext:
    pages = []


def test_send_requires_approver():
    teams = TeamsWebAutomation(
        FakeContext(),
        "https://teams.microsoft.com/",
        (AllowedChat(id="allowed", name="Allowed"),),
    )
    try:
        teams.send("allowed", "Text", "")
    except TeamsSafetyError:
        return
    raise AssertionError("Send without an approver was accepted")

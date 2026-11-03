import pytest

from automation.teams_web import TeamsSafetyError, require_teams_origin


@pytest.mark.parametrize(
    "url",
    (
        "https://teams.microsoft.com/",
        "https://teams.cloud.microsoft/",
        "https://teams.cloud.microsoft.mcas.ms/",
    ),
)
def test_official_and_observed_proxy_hosts_are_allowed(url):
    require_teams_origin(url)


@pytest.mark.parametrize(
    "url",
    (
        "https://login.microsoftonline.com/",
        "https://teams.example.com/",
        "http://teams.microsoft.com/",
    ),
)
def test_unknown_or_login_origin_is_rejected(url):
    with pytest.raises(TeamsSafetyError):
        require_teams_origin(url)


def test_open_chat_stops_before_inspecting_login_page():
    from automation.teams_web import TeamsWebAutomation
    from config.settings import AllowedChat

    class LoginPage:
        url = "https://login.microsoftonline.com/"

        def get_by_text(self, *_args, **_kwargs):
            raise AssertionError("Login page must not be inspected for chat text")

    class Context:
        pages = [LoginPage()]

    teams = TeamsWebAutomation(
        Context(),
        "https://teams.microsoft.com/",
        (AllowedChat(id="authorized", name="Authorized chat"),),
    )

    with pytest.raises(TeamsSafetyError):
        teams.open_chat("authorized")

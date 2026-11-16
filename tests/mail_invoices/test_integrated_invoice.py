from types import SimpleNamespace

import pytest

from mail_invoices import movement


@pytest.mark.parametrize("approved,eligible", [(False, True), (True, False), (False, False)])
def test_movement_gate_never_touches_page_without_both_approvals(approved, eligible):
    with pytest.raises(ValueError, match="registration"):
        movement.move_registered_message(None, {"eligible": eligible}, "Salud", approved=approved)


def test_destination_waits_for_async_tree_before_validating():
    events = []

    class Roots:
        @property
        def first(self):
            return self

        def wait_for(self, *, state):
            assert state == "visible"
            events.append("loaded")

        def count(self):
            assert events == ["loaded"]
            return 0

    dialog = SimpleNamespace(locator=lambda selector: Roots())
    with pytest.raises(LookupError, match="mailbox"):
        movement.select_move_destination(dialog, "Sura - Comfama")

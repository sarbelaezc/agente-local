from __future__ import annotations

OUTLOOK_URL = "https://outlook.live.com/mail/0/"
# Wide enough for Outlook's three-column layout; at narrow widths the message list is hidden.
OUTLOOK_VIEWPORT = {"width": 1920, "height": 1000}


def outlook_page(context):
    """Keeps one tab and navigates it to Outlook; closes blank and restored extra tabs."""
    pages = list(context.pages)
    page = pages[0] if pages else context.new_page()
    for extra in pages[1:]:
        extra.close()
    page.set_viewport_size(OUTLOOK_VIEWPORT)
    page.goto(OUTLOOK_URL, wait_until="domcontentloaded")
    return page


def reopen_outlook(page, ready_selector: str):
    """Reloads the inbox so each candidate starts from a fresh, unscrolled list."""
    page.goto(OUTLOOK_URL, wait_until="domcontentloaded")
    page.wait_for_selector(ready_selector, timeout=60000)
    return page

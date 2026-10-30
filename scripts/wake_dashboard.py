"""Keep the Streamlit Community Cloud dashboard awake.

Free Streamlit apps go to sleep after ~12 hours without a visitor and only a
real browser visit (clicking "Yes, get this app back up!") wakes them. The
scheduler workflow runs this a few times a day with headless Chromium.

Usage: python scripts/wake_dashboard.py <app url>
"""
import sys

from playwright.sync_api import sync_playwright

WAKE_BUTTON = "Yes, get this app back up"


def main(url):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(15_000)
        button = page.get_by_role("button", name=WAKE_BUTTON)
        if button.count():
            print("dashboard was asleep - waking it")
            button.first.click()
            page.wait_for_timeout(60_000)
        else:
            print("dashboard is awake")
        # the app itself renders inside an iframe; give it time to register the session
        page.wait_for_timeout(10_000)
        print("final url:", page.url)
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1])

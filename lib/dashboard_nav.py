"""The page nav that opens every served dashboard page.

One list of pages, so a page added here reaches every page's header at once
and none can drift out of the set. Its styling is `.page-nav` in bin/dashboard's
shared CSS, which every page already inlines.
"""
from __future__ import annotations

import html

# (key, href, label), in nav order.
PAGES = (("dashboard", "/", "Dashboard"),
         ("v2", "/dashboard-v2.html", "Dashboard v2"),
         ("estate", "/estate.html", "Estate"),
         ("stewards", "/stewards.html", "Stewards"))


def render(active: str) -> str:
    """One link per page with the current one marked, and the theme toggle at
    the far end — the same bar, in the same place, on every page."""
    on = ' class="pn-link on" aria-current="page"'
    off = ' class="pn-link"'
    links = "".join(
        f'<a{on if key == active else off} href="{href}">{html.escape(label)}</a>'
        for key, href, label in PAGES)
    return (f'<nav class="page-nav" aria-label="Dashboard pages">{links}'
            f'<span class="pn-grow"></span>'
            f'<button class="themebtn" id="themebtn" type="button" '
            f'aria-label="Toggle theme">☾ Night shift</button></nav>')

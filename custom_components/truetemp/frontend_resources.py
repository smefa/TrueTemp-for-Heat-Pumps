"""Keep one bundled script listed in the Lovelace resources collection.

`add_extra_js_url` puts a script in the index page, but the browser's service
worker serves a cached copy of that page first. A copy taken before the
script was registered (an older TrueTemp, or a page fetched while a slow box
was still starting) never loads it. Cards survive that because they redraw
once their element shows up later; a dashboard strategy does not, as the
frontend gives a custom strategy a hard 5 s to register and then shows
"Timeout waiting for strategy element". The resources collection is fetched
fresh over the websocket each time a dashboard opens, just before that wait,
so listing the strategy script there too removes the dependency on the page.

No Home Assistant import, so the logic is unit-testable without HA: it only
needs an object with the `ResourceStorageCollection` methods used below.
"""

from __future__ import annotations

from typing import Any

RESOURCE_TYPE = "module"


def _path(url: str) -> str:
    return url.split("?", 1)[0]


async def async_sync_resource(collection: Any, url: str) -> str | None:
    """Make `collection` list `url` exactly once, as a module.

    Entries are matched on the path without the query string, so an entry
    from an older release (other `?v=`) is updated in place rather than kept
    alongside, and a hand-added copy of the same script is adopted. Returns
    "created", "updated" or None when nothing had to change.
    """
    await collection.async_get_info()  # loads the stored items on first use
    matches = [
        item for item in collection.async_items() if _path(item.get("url", "")) == _path(url)
    ]
    if not matches:
        await collection.async_create_item({"res_type": RESOURCE_TYPE, "url": url})
        return "created"
    first, *duplicates = matches
    for item in duplicates:
        await collection.async_delete_item(item["id"])
    if first.get("url") != url or first.get("type") != RESOURCE_TYPE:
        await collection.async_update_item(first["id"], {"res_type": RESOURCE_TYPE, "url": url})
        return "updated"
    return "updated" if duplicates else None


async def async_remove_resource(collection: Any, url: str) -> bool:
    """Drop every entry for `url`'s path. Returns True if any was removed."""
    await collection.async_get_info()
    matches = [
        item for item in collection.async_items() if _path(item.get("url", "")) == _path(url)
    ]
    for item in matches:
        await collection.async_delete_item(item["id"])
    return bool(matches)

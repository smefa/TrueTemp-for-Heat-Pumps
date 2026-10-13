"""Unit tests for keeping the dashboard script in the Lovelace resources.

A fake stands in for HA's `ResourceStorageCollection`, exposing only the
methods `frontend_resources.py` calls.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

fr = load("frontend_resources")

URL = "/truetemp/truetemp-dashboard.js?v=2"


class FakeCollection:
    def __init__(self, items=None):
        self.items = {item["id"]: dict(item) for item in items or []}
        self.next_id = 100

    async def async_get_info(self):
        return {"resources": len(self.items)}

    def async_items(self):
        return list(self.items.values())

    async def async_create_item(self, data):
        item_id = str(self.next_id)
        self.next_id += 1
        self.items[item_id] = {"id": item_id, "type": data["res_type"], "url": data["url"]}
        return self.items[item_id]

    async def async_update_item(self, item_id, updates):
        self.items[item_id].update(type=updates["res_type"], url=updates["url"])
        return self.items[item_id]

    async def async_delete_item(self, item_id):
        del self.items[item_id]


OTHER = {"id": "1", "type": "module", "url": "/hacsfiles/card/card.js?hacstag=1"}


def test_creates_when_missing():
    col = FakeCollection([OTHER])
    assert asyncio.run(fr.async_sync_resource(col, URL)) == "created"
    urls = sorted(i["url"] for i in col.async_items())
    assert urls == sorted([OTHER["url"], URL])


def test_no_change_when_current():
    col = FakeCollection([OTHER, {"id": "2", "type": "module", "url": URL}])
    assert asyncio.run(fr.async_sync_resource(col, URL)) is None
    assert len(col.items) == 2


def test_updates_old_version_in_place():
    old = "/truetemp/truetemp-dashboard.js?v=1"
    col = FakeCollection([{"id": "2", "type": "module", "url": old}])
    assert asyncio.run(fr.async_sync_resource(col, URL)) == "updated"
    assert col.items == {"2": {"id": "2", "type": "module", "url": URL}}


def test_adopts_hand_added_entry_and_drops_duplicates():
    col = FakeCollection(
        [
            {"id": "2", "type": "js", "url": "/truetemp/truetemp-dashboard.js"},
            {"id": "3", "type": "module", "url": URL},
            OTHER,
        ]
    )
    assert asyncio.run(fr.async_sync_resource(col, URL)) == "updated"
    assert col.items == {"2": {"id": "2", "type": "module", "url": URL}, "1": OTHER}


def test_remove_only_touches_our_entry():
    col = FakeCollection([OTHER, {"id": "2", "type": "module", "url": URL}])
    assert asyncio.run(fr.async_remove_resource(col, URL)) is True
    assert col.items == {"1": OTHER}
    assert asyncio.run(fr.async_remove_resource(col, URL)) is False

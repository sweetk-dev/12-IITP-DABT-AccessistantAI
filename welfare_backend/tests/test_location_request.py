"""위치 1회 요청 (v2.19.0) — 정책상담 화면은 위치를 계속 보내지 않고, '근처' 도구가 불릴 때만 묻는다."""
import asyncio
import time

import pytest

from nav_context import needs_current_location
import live_bridge


@pytest.mark.parametrize("fname", ["find_toilet", "find_culture_sports", "find_accessible_restaurants",
                                   "find_emergency_support", "find_nearby_transit",
                                   "check_building_accessibility", "find_bf_tour_spots"])
def test_nearby_tools_need_location_without_place(fname):
    assert needs_current_location(fname, {}, {}) is True
    assert needs_current_location(fname, {"place": "안양역"}, {}) is False


def test_bus_arrivals_needs_location_only_without_station():
    assert needs_current_location("get_bus_arrivals", {}, {}) is True
    assert needs_current_location("get_bus_arrivals", {"station_id": "1"}, {}) is False
    assert needs_current_location("get_bus_arrivals", {"place": "안양역"}, {}) is False
    assert needs_current_location("get_bus_arrivals", {}, {"board_station_id": "2"}) is False


def test_policy_tools_do_not_need_location():
    for f in ("search_by_keyword", "get_policy_detail", "plan_accessible_route", "unknown_tool"):
        assert needs_current_location(f, {}, {}) is False


class _WS:
    def __init__(self, reply=None, delay=0.01):
        self.sent, self.reply, self.delay = [], reply, delay

    async def send_json(self, p):
        self.sent.append(p)


def test_request_location_once_waits_for_reply(monkeypatch):
    async def run():
        loc = {"lat": None, "lng": None, "ts": 0.0}
        ev = asyncio.Event()
        ws = _WS()

        async def answer():
            await asyncio.sleep(0.05)
            loc.update(lat=37.39, lng=126.95, ts=time.time())
            ev.set()
        asyncio.create_task(answer())
        ok = await live_bridge._request_location_once(ws, loc, ev)
        return ok, ws.sent
    ok, sent = asyncio.run(run())
    assert ok is True
    assert sent == [{"type": "location_request"}]


def test_request_location_once_times_out(monkeypatch):
    monkeypatch.setattr(live_bridge, "LOCATION_WAIT_SEC", 0.05)

    async def run():
        loc = {"lat": None, "lng": None, "ts": 0.0}
        return await live_bridge._request_location_once(_WS(), loc, asyncio.Event())
    assert asyncio.run(run()) is False


def test_request_location_once_unavailable_releases_and_backs_off():
    async def run():
        loc = {"lat": None, "lng": None, "ts": 0.0}
        ev = asyncio.Event()

        async def answer():
            await asyncio.sleep(0.02)
            ev.set()                       # location_unavailable — 위치 없이 기다림만 푼다
        asyncio.create_task(answer())
        t0 = asyncio.get_event_loop().time()
        ok = await live_bridge._request_location_once(_WS(), loc, ev)
        return ok, asyncio.get_event_loop().time() - t0, loc
    ok, waited, loc = asyncio.run(run())
    assert ok is False and waited < 1.0
    assert loc.get("fail_ts")
    # 실패 직후에는 다시 묻지 않는다
    assert live_bridge.location_request_due(loc, loc["fail_ts"] + 5) is False
    assert live_bridge.location_request_due(loc, loc["fail_ts"] + live_bridge.LOCATION_RETRY_SEC + 1) is True


def test_location_request_due_fresh_and_stale():
    now = 1000.0
    assert live_bridge.location_request_due({"lat": None}, now) is True
    assert live_bridge.location_request_due({"lat": 37.0, "lng": 127.0, "ts": now - 10}, now) is False
    assert live_bridge.location_request_due({"lat": 37.0, "lng": 127.0, "ts": now - 500}, now) is True


def test_request_location_once_send_failure(monkeypatch):
    class _Dead:
        async def send_json(self, p):
            raise RuntimeError("closed")

    async def run():
        loc = {"lat": None, "lng": None, "ts": 0.0}
        return await live_bridge._request_location_once(_Dead(), loc, asyncio.Event()), loc
    ok, loc = asyncio.run(run())
    assert ok is False and loc.get("fail_ts")

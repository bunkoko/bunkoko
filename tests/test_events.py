import pandas as pd

from cfdbot.events import (
    Event,
    EventIndex,
    friday_cutoff_passed,
    recurring_oil_events,
    server_to_utc,
    trading_day,
)


def _jst(e):
    return e.time.tz_convert("Asia/Tokyo")


def test_eia_times_summer_and_winter():
    ev = recurring_oil_events(pd.Timestamp("2026-09-28", tz="UTC"), pd.Timestamp("2026-12-06", tz="UTC"))
    eia = {e.time.tz_convert("America/New_York").date().isoformat(): e for e in ev if e.name == "EIA"}
    summer = eia["2026-09-30"]
    assert summer.time == pd.Timestamp("2026-09-30 14:30", tz="UTC")
    assert _jst(summer).strftime("%a %H:%M") == "Wed 23:30"
    winter = eia["2026-12-02"]
    assert winter.time == pd.Timestamp("2026-12-02 15:30", tz="UTC")
    assert _jst(winter).strftime("%a %H:%M") == "Thu 00:30"
    api = [e for e in ev if e.name == "API"][0]
    assert api.time.tz_convert("America/New_York").strftime("%a %H:%M") == "Tue 16:30"


def test_event_index_windows():
    t = pd.Timestamp("2026-09-30 14:30", tz="UTC")
    idx = EventIndex([Event(t, "EIA", "oil")])
    h = pd.Timedelta(hours=1)
    assert idx.in_window(t - 3 * h, ["oil"], 4 * h, h)
    assert idx.in_window(t + 0.5 * h, ["oil"], 4 * h, h)
    assert not idx.in_window(t + 2 * h, ["oil"], 4 * h, h)
    assert not idx.in_window(t - 5 * h, ["oil"], 4 * h, h)
    assert not idx.in_window(t, ["usd_macro"], 4 * h, h)
    assert idx.upcoming(t - 2 * h, ["oil"], 4 * h)
    assert not idx.upcoming(t, ["oil"], 4 * h)


def test_friday_cutoff():
    et = "America/New_York"
    assert not friday_cutoff_passed(pd.Timestamp("2026-10-02 11:00", tz=et), 12)
    assert friday_cutoff_passed(pd.Timestamp("2026-10-02 12:00", tz=et), 12)
    assert friday_cutoff_passed(pd.Timestamp("2026-10-03 12:00", tz=et), 12)
    assert friday_cutoff_passed(pd.Timestamp("2026-10-04 17:59", tz=et), 12)
    assert not friday_cutoff_passed(pd.Timestamp("2026-10-04 18:00", tz=et), 12)


def test_trading_day_rolls_at_17_et():
    et = "America/New_York"
    assert trading_day(pd.Timestamp("2026-09-29 16:59", tz=et)) == pd.Timestamp("2026-09-29")
    assert trading_day(pd.Timestamp("2026-09-29 17:00", tz=et)) == pd.Timestamp("2026-09-30")


def test_server_time_ny_close():
    idx = pd.DatetimeIndex(["2026-07-01 00:00", "2026-01-05 00:00"])
    utc = server_to_utc(idx, "ny_close")
    assert utc[0] == pd.Timestamp("2026-06-30 21:00", tz="UTC")  # 夏: 17:00 EDT
    assert utc[1] == pd.Timestamp("2026-01-04 22:00", tz="UTC")  # 冬: 17:00 EST
    assert server_to_utc(pd.DatetimeIndex(["2026-01-05 09:00"]), 9)[0] == pd.Timestamp(
        "2026-01-05 00:00", tz="UTC"
    )

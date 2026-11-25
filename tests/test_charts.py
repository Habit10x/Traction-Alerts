from datetime import datetime, timezone

import pandas as pd

from habit10x_marketing_automation.charts import alert_chart, history_chart
from habit10x_marketing_automation.engine import Firing
from habit10x_marketing_automation.rules import Event

UTC = timezone.utc


def test_history_chart_has_a_time_and_a_value_for_every_check_shown_in_ist():
    chart = history_chart(pd.Series(["2026-11-25T05:30:00+00:00", "2026-11-25T11:30:00+00:00"]),
                          pd.Series([100, 160]), "Views")
    data = chart.data
    assert list(data.columns) == ["time", "value"]  # the chart encodes exactly these two fields
    assert list(data["value"]) == [100.0, 160.0]
    assert list(data["time"]) == [pd.Timestamp("2026-11-25 11:00"), pd.Timestamp("2026-11-25 17:00")]  # UTC + 5:30


def test_history_chart_drops_checks_where_the_platform_gave_no_number():
    chart = history_chart(pd.Series(["2026-11-25T05:30:00+00:00", "2026-11-25T06:30:00+00:00"]),
                          pd.Series([None, 7]), "Comments")
    assert list(chart.data["value"]) == [7.0]


def test_history_chart_survives_a_single_point_and_builds_a_valid_spec():
    chart = history_chart(pd.Series(["2026-11-25T05:30:00+00:00"]), pd.Series([5]), "Likes")
    assert chart.to_dict()["mark"]["type"] == "line"


def test_alert_chart_draws_the_series_the_threshold_line_and_the_fire_points():
    t = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)
    series = [(t, 0), (t.replace(minute=15), 3), (t.replace(minute=30), 1)]
    fired = [Firing("r", "", 3, [Event(t, "", "a", {})], t.replace(minute=15))]
    chart = alert_chart(series, fired, threshold=2)
    area, limit, dots = chart.layer
    assert list(area.data.columns) == ["time", "count"] and list(area.data["count"]) == [0, 3, 1]
    assert list(limit.data["threshold"]) == [2]
    assert list(dots.data["count"]) == [3] and dots.data["time"].iloc[0] == pd.Timestamp("2026-11-25 11:45")
    assert len(alert_chart(series, [], threshold=2).layer) == 2  # no dots when nothing would have fired

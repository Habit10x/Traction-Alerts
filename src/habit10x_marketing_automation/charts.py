"""Altair charts used by the dashboard (kept apart from the Streamlit script so they can be tested)."""

import altair as alt
import pandas as pd

IST = "Asia/Kolkata"


def _to_ist(values) -> pd.Series:
    return pd.to_datetime(pd.Series(values), utc=True).dt.tz_convert(IST).dt.tz_localize(None)


def history_chart(times, values, label: str) -> alt.Chart:
    """One metric over the checks. The axis zooms to the data so small growth is still visible."""
    data = pd.DataFrame({"time": _to_ist(times), "value": pd.Series(values, dtype="float")}).dropna()
    return alt.Chart(data).mark_line(point=True).encode(
        x=alt.X("time:T", title=None),
        y=alt.Y("value:Q", title=None, scale=alt.Scale(zero=False), axis=alt.Axis(tickMinStep=1, format=",d")),
        tooltip=[alt.Tooltip("time:T", title="Checked (IST)", format="%d %b %H:%M"),
                 alt.Tooltip("value:Q", title=label, format=",")]).properties(height=220)


def alert_chart(series: list, fired: list, threshold: int) -> alt.Chart:
    """The count the checker would have seen over time, the threshold line, and a dot where an email would go out."""
    df = pd.DataFrame(series, columns=["time", "count"])
    df["time"] = _to_ist(df["time"])
    ymax = max(int(df["count"].max()) if len(df) else 0, threshold, 1) * 1.15
    area = alt.Chart(df).mark_area(interpolate="step-after", opacity=0.35, line=True).encode(
        x=alt.X("time:T", title=None),
        y=alt.Y("count:Q", title=None, scale=alt.Scale(domain=[0, ymax]), axis=alt.Axis(tickMinStep=1, format="d")),
        tooltip=[alt.Tooltip("time:T", title="Time (IST)", format="%d %b %H:%M"), alt.Tooltip("count:Q", title="Count")])
    limit = alt.Chart(pd.DataFrame({"threshold": [threshold]})).mark_rule(color="#dc2626", strokeDash=[6, 4]).encode(
        y="threshold:Q")
    layers = area + limit
    if fired:
        dots = pd.DataFrame({"time": _to_ist([f.newest for f in fired]), "count": [f.count for f in fired]})
        layers += alt.Chart(dots).mark_point(filled=True, color="#dc2626", size=70).encode(
            x="time:T", y="count:Q",
            tooltip=[alt.Tooltip("time:T", title="Would email (IST)", format="%d %b %H:%M"),
                     alt.Tooltip("count:Q", title="Count")])
    return layers.properties(height=200)

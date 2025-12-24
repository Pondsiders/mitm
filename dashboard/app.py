"""
Alpha Usage Dashboard - Streamlit app for monitoring Claude API usage.

The model: Use the reset timestamp from the API as ground truth.
Count driving hours (6 AM - 6 PM Pacific) between now and reset.
Compare actual burn rate to sustainable rate.
"""

import pandas as pd
import streamlit as st
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

# Config
CSV_PATH = Path("/data/quota.csv")
REFRESH_SECONDS = 30
PACIFIC = ZoneInfo("America/Los_Angeles")
UTC = ZoneInfo("UTC")

# Driving hours: 6 AM to 6 PM Pacific
DRIVING_START_HOUR = 6
DRIVING_END_HOUR = 18


def count_driving_hours(start: datetime, end: datetime) -> float:
    """Count 6am-6pm Pacific hours between two timestamps."""
    # Convert to Pacific for driving window calculations
    start_pacific = start.astimezone(PACIFIC)
    end_pacific = end.astimezone(PACIFIC)

    hours = 0.0
    current = start_pacific

    while current < end_pacific:
        day_6am = current.replace(hour=DRIVING_START_HOUR, minute=0, second=0, microsecond=0)
        day_6pm = current.replace(hour=DRIVING_END_HOUR, minute=0, second=0, microsecond=0)

        # If we're past today's driving window, skip to tomorrow 6am
        if current >= day_6pm:
            next_day = current + timedelta(days=1)
            current = next_day.replace(hour=DRIVING_START_HOUR, minute=0, second=0, microsecond=0)
            continue

        # If we're before today's driving window, fast forward to 6am
        if current < day_6am:
            current = day_6am

        # Now we're in the driving window - count hours until 6pm or end, whichever first
        window_end = min(end_pacific, day_6pm)
        if window_end > current:
            hours += (window_end - current).total_seconds() / 3600

        # Move to next day 6am
        next_day = current + timedelta(days=1)
        current = next_day.replace(hour=DRIVING_START_HOUR, minute=0, second=0, microsecond=0)

    return hours


def get_driving_hours_until_6pm(now_pacific: datetime) -> float:
    """Get driving hours remaining until 6 PM today."""
    six_pm = now_pacific.replace(hour=DRIVING_END_HOUR, minute=0, second=0, microsecond=0)
    six_am = now_pacific.replace(hour=DRIVING_START_HOUR, minute=0, second=0, microsecond=0)

    if now_pacific >= six_pm:
        return 0.0
    elif now_pacific < six_am:
        return (six_pm - six_am).total_seconds() / 3600
    else:
        return (six_pm - now_pacific).total_seconds() / 3600


def get_burn_rate(df: pd.DataFrame, lookback_hours: float = 2.0) -> tuple[float, float]:
    """Calculate burn rate from recent data.

    Returns (rate_per_hour, driving_hours_in_window).
    Rate is in percentage points per driving hour.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=lookback_hours)

    recent = df[df['timestamp'] >= cutoff]
    if len(recent) < 2:
        return 0.0, 0.0

    first = recent.iloc[0]
    last = recent.iloc[-1]

    usage_delta = last['anthropic-ratelimit-unified-7d-utilization'] - first['anthropic-ratelimit-unified-7d-utilization']
    time_delta_hours = (last['timestamp'] - first['timestamp']).total_seconds() / 3600

    if time_delta_hours <= 0:
        return 0.0, 0.0

    # Calculate driving hours in this window
    driving_hours = count_driving_hours(first['timestamp'].to_pydatetime(), last['timestamp'].to_pydatetime())

    if driving_hours <= 0:
        # All activity was outside driving hours (overnight)
        return 0.0, 0.0

    rate = usage_delta / driving_hours
    return rate, driving_hours


# === Streamlit App ===

st.set_page_config(
    page_title="Alpha Usage",
    page_icon="🦆",
    layout="centered",
)

st.title("🦆 Alpha Usage Dashboard")


@st.cache_data(ttl=REFRESH_SECONDS)
def load_data():
    """Load and parse the quota CSV."""
    if not CSV_PATH.exists():
        return None
    df = pd.read_csv(CSV_PATH)
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    return df


# Load data
df = load_data()

if df is None or len(df) == 0:
    st.warning("No data yet. Start using Claude Code through the proxy!")
    st.stop()

# Get latest values
latest = df.iloc[-1]
util_7d = latest['anthropic-ratelimit-unified-7d-utilization']
util_5h = latest['anthropic-ratelimit-unified-5h-utilization']
reset_timestamp = int(latest['anthropic-ratelimit-unified-7d-reset'])
reset_dt = datetime.fromtimestamp(reset_timestamp, tz=UTC)

# Current time
now = datetime.now(UTC)
now_pacific = now.astimezone(PACIFIC)

# Core calculations
budget_remaining = 1.0 - util_7d  # As decimal (e.g., 0.97 = 97% remaining)
driving_hours_remaining = count_driving_hours(now, reset_dt)
hours_until_6pm = get_driving_hours_until_6pm(now_pacific)

# Actual burn rate from recent data
actual_rate, rate_window_hours = get_burn_rate(df, lookback_hours=3.0)

# Project usage at reset based on current rate
if rate_window_hours >= 0.5 and actual_rate > 0 and driving_hours_remaining > 0:
    projected_at_reset = util_7d + (actual_rate * driving_hours_remaining)
else:
    projected_at_reset = None  # Not enough data to project

# Verdict based on projection
# 100% = THE WALL. Over = bad. Under = safety margin.
if projected_at_reset is None:
    pace_status = "no_data"
    pace_message = "Not enough data to project"
elif projected_at_reset > 1.0:
    # Will hit the wall
    pace_status = "wall"
    # Calculate when we'll hit 100%
    remaining_to_wall = 1.0 - util_7d
    hours_to_wall = remaining_to_wall / actual_rate if actual_rate > 0 else 0
    pace_message = f"Will hit wall in ~{hours_to_wall:.0f} driving hours"
elif projected_at_reset > 0.95:
    pace_status = "tight"
    margin = (1.0 - projected_at_reset) * 100
    pace_message = f"Tight — only {margin:.1f}% margin"
elif projected_at_reset > 0.80:
    pace_status = "comfortable"
    margin = (1.0 - projected_at_reset) * 100
    pace_message = f"Comfortable — {margin:.0f}% margin"
else:
    pace_status = "runway"
    margin = (1.0 - projected_at_reset) * 100
    pace_message = f"Plenty of runway — {margin:.0f}% margin"

# === Display ===

st.subheader("Budget Status")

col1, col2, col3 = st.columns(3)

with col1:
    st.metric(
        label="Used",
        value=f"{util_7d * 100:.1f}%",
    )

with col2:
    st.metric(
        label="Remaining",
        value=f"{budget_remaining * 100:.1f}%",
    )

with col3:
    st.metric(
        label="Driving Hours Left",
        value=f"{driving_hours_remaining:.1f}h",
    )

# Projection
st.divider()
st.subheader("Projection")

if projected_at_reset is not None:
    col1, col2 = st.columns(2)
    with col1:
        st.metric(
            label="Projected at Reset",
            value=f"{projected_at_reset * 100:.1f}%",
        )
    with col2:
        st.metric(
            label="The Wall",
            value="100%",
        )

# Status indicator
if pace_status == "wall":
    st.error(f"🔴 {pace_message}")
elif pace_status == "tight":
    st.warning(f"🟡 {pace_message}")
elif pace_status == "comfortable":
    st.success(f"🟢 {pace_message}")
elif pace_status == "runway":
    st.success(f"💨 {pace_message}")
else:
    st.info(f"ℹ️ {pace_message}")

# Rate details (smaller, for nerds)
if actual_rate > 0:
    st.caption(f"Current burn rate: {actual_rate * 100:.2f}%/driving hour")

# Today's info
st.divider()
st.subheader("Today")

col1, col2 = st.columns(2)
with col1:
    st.metric(label="Driving hours until 6 PM", value=f"{hours_until_6pm:.1f}h")
with col2:
    if hours_until_6pm == 0:
        st.info("🌙 Outside driving hours")
    elif actual_rate > 0:
        projected_by_6pm = util_7d + (actual_rate * hours_until_6pm)
        st.metric(label="Projected by 6 PM", value=f"{projected_by_6pm * 100:.1f}%")

# 5-hour burst protection
st.divider()
st.subheader("Burst Protection (5h)")

col1, col2 = st.columns(2)

with col1:
    st.metric(label="5-Hour Window", value=f"{util_5h * 100:.1f}%")
    st.progress(min(util_5h, 1.0))

with col2:
    if util_5h > 0.8:
        st.error("🔥 Slow down!")
    elif util_5h > 0.5:
        st.warning("⚠️ Moderate pace")
    else:
        st.success("✅ Comfortable")

# Historical chart
st.divider()
st.subheader("Usage Over Time")

chart_df = df[['timestamp', 'anthropic-ratelimit-unified-7d-utilization']].copy()
chart_df.columns = ['timestamp', 'Usage']
chart_df = chart_df.set_index('timestamp')
chart_df = chart_df * 100

st.line_chart(chart_df, height=200)

# Footer
st.divider()
reset_pacific = reset_dt.astimezone(PACIFIC)
st.caption(f"Window resets: {reset_pacific.strftime('%a %b %d, %-I:%M %p')} Pacific")
st.caption(f"Last updated: {latest['timestamp'].strftime('%Y-%m-%d %H:%M:%S')} UTC")
st.caption(f"Data points: {len(df)}")

# Auto-refresh
st.button("🔄 Refresh")

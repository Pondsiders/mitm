"""
Alpha Usage Dashboard - Streamlit app for monitoring Claude API usage.

The model: 84 driving hours per week (6 AM - 6 PM Pacific, 7 days).
Week starts Monday 11 AM Pacific. Each day has a cumulative target.
"""

import pandas as pd
import streamlit as st
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

# Config
CSV_PATH = Path("/data/quota.csv")
REFRESH_SECONDS = 30
PACIFIC = ZoneInfo("America/Los_Angeles")

# Driving hours: 6 AM to 6 PM = 12 hours per day
# But Monday starts at 11 AM, so Monday = 7 hours
# Week: Mon(7) + Tue-Sun(12*6=72) + Mon morning 6-10:59(5) = 84 hours
# Cumulative targets at END of each day (6 PM Pacific):
#   Monday EOD:    7 hrs  ->  7/84  =  8.33%
#   Tuesday EOD:  19 hrs  -> 19/84  = 22.62%
#   Wednesday EOD: 31 hrs -> 31/84  = 36.90%
#   Thursday EOD:  43 hrs -> 43/84  = 51.19%
#   Friday EOD:    55 hrs -> 55/84  = 65.48%
#   Saturday EOD:  67 hrs -> 67/84  = 79.76%
#   Sunday EOD:    79 hrs -> 79/84  = 94.05%
#   Monday 10:59:  84 hrs -> 84/84  = 100%

CUMULATIVE_HOURS = {
    0: 7,   # Monday EOD (11 AM - 6 PM = 7 hrs)
    1: 19,  # Tuesday EOD
    2: 31,  # Wednesday EOD
    3: 43,  # Thursday EOD
    4: 55,  # Friday EOD
    5: 67,  # Saturday EOD
    6: 79,  # Sunday EOD
}
TOTAL_DRIVING_HOURS = 84


def get_week_start(now_pacific: datetime) -> datetime:
    """Get the Monday 11 AM Pacific that starts this week's window."""
    # Find most recent Monday
    days_since_monday = now_pacific.weekday()  # Monday = 0
    monday = now_pacific - timedelta(days=days_since_monday)
    monday_11am = monday.replace(hour=11, minute=0, second=0, microsecond=0)

    # If we're before Monday 11 AM, go back a week
    if now_pacific < monday_11am:
        monday_11am -= timedelta(days=7)

    return monday_11am


def get_driving_hours_elapsed(now_pacific: datetime, week_start: datetime) -> float:
    """Calculate driving hours elapsed since week start."""
    hours = 0.0
    current = week_start

    while current < now_pacific:
        current_day = current.weekday()
        day_start = current.replace(hour=6, minute=0, second=0, microsecond=0)
        day_end = current.replace(hour=18, minute=0, second=0, microsecond=0)

        # Special case: Monday starts at 11 AM, not 6 AM
        if current_day == 0 and current.date() == week_start.date():
            day_start = current.replace(hour=11, minute=0, second=0, microsecond=0)

        # Calculate driving hours for this day
        if now_pacific.date() == current.date():
            # Today - partial day
            if now_pacific < day_start:
                pass  # Before driving hours
            elif now_pacific > day_end:
                hours += (day_end - max(day_start, current)).total_seconds() / 3600
            else:
                hours += (now_pacific - max(day_start, current)).total_seconds() / 3600
            break
        else:
            # Full day (within driving window)
            if current <= day_end:
                hours += (day_end - max(day_start, current)).total_seconds() / 3600
            current = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0)

    return max(0, hours)


def get_target_now(now_pacific: datetime, week_start: datetime) -> float:
    """Get the target utilization for right now (based on driving hours elapsed)."""
    hours = get_driving_hours_elapsed(now_pacific, week_start)
    return hours / TOTAL_DRIVING_HOURS


def get_target_eod(now_pacific: datetime) -> float:
    """Get target utilization at 6 PM today."""
    weekday = now_pacific.weekday()
    return CUMULATIVE_HOURS.get(weekday, 79) / TOTAL_DRIVING_HOURS


def get_hours_until_6pm(now_pacific: datetime) -> float:
    """Get driving hours remaining until 6 PM today."""
    six_pm = now_pacific.replace(hour=18, minute=0, second=0, microsecond=0)
    six_am = now_pacific.replace(hour=6, minute=0, second=0, microsecond=0)

    # Special case: Monday, driving starts at 11 AM
    if now_pacific.weekday() == 0:
        six_am = now_pacific.replace(hour=11, minute=0, second=0, microsecond=0)

    if now_pacific >= six_pm:
        return 0.0
    elif now_pacific < six_am:
        return (six_pm - six_am).total_seconds() / 3600
    else:
        return (six_pm - now_pacific).total_seconds() / 3600


def calculate_projection(current_util: float, target_eod: float, hours_left: float,
                         start_of_day_util: float, hours_driven_today: float) -> dict:
    """Calculate when we'll hit today's target."""
    remaining_budget = target_eod - current_util
    used_today = current_util - start_of_day_util

    if hours_driven_today <= 0:
        # No data for today yet
        return {
            'status': 'no_data',
            'message': "No driving data today yet",
        }

    burn_rate = used_today / hours_driven_today  # % per hour

    if burn_rate <= 0:
        return {
            'status': 'banking',
            'message': f"Banking time — {remaining_budget*100:.1f}% headroom",
            'remaining': remaining_budget,
        }

    hours_to_exhaust = remaining_budget / burn_rate

    if hours_to_exhaust >= hours_left:
        # On track
        projected_eod = current_util + (burn_rate * hours_left)
        headroom = target_eod - projected_eod
        return {
            'status': 'on_track',
            'message': f"On track — {headroom*100:.1f}% headroom at 6 PM",
            'remaining': remaining_budget,
            'projected_eod': projected_eod,
        }
    else:
        # Will exhaust before 6 PM
        from datetime import datetime as dt
        now = datetime.now(PACIFIC)
        exhaust_time = now + timedelta(hours=hours_to_exhaust)
        return {
            'status': 'exhausted',
            'message': f"Budget exhausted at {exhaust_time.strftime('%-I:%M %p')}",
            'exhaust_time': exhaust_time,
            'remaining': remaining_budget,
        }


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

# Current time in Pacific
now = datetime.now(PACIFIC)
week_start = get_week_start(now)

# Calculate targets
target_now = get_target_now(now, week_start)
target_eod = get_target_eod(now)
hours_left = get_hours_until_6pm(now)

# Get start-of-day utilization (find first record after 6 AM today)
today_6am = now.replace(hour=6, minute=0, second=0, microsecond=0)
if now.weekday() == 0:  # Monday
    today_6am = now.replace(hour=11, minute=0, second=0, microsecond=0)

today_data = df[df['timestamp'] >= today_6am.astimezone(ZoneInfo('UTC'))]
if len(today_data) > 0:
    start_of_day_util = today_data.iloc[0]['anthropic-ratelimit-unified-7d-utilization']
    hours_driven_today = get_driving_hours_elapsed(now, week_start) - get_driving_hours_elapsed(
        today_6am, week_start)
else:
    start_of_day_util = util_7d  # Fallback
    hours_driven_today = 0

# Position: ahead or behind?
position = target_now - util_7d  # Positive = ahead, negative = behind

# === Display ===

# Main status
st.subheader("Today's Budget")

col1, col2, col3 = st.columns(3)

with col1:
    st.metric(
        label="Current",
        value=f"{util_7d * 100:.2f}%",
    )

with col2:
    st.metric(
        label="Target (now)",
        value=f"{target_now * 100:.2f}%",
    )

with col3:
    delta_str = f"{position * 100:+.2f}%"
    if position >= 0:
        st.metric(label="Position", value="Ahead", delta=delta_str)
    else:
        st.metric(label="Position", value="Behind", delta=delta_str, delta_color="inverse")

# Projection
st.divider()
projection = calculate_projection(util_7d, target_eod, hours_left, start_of_day_util, hours_driven_today)

if projection['status'] == 'on_track':
    st.success(f"✅ {projection['message']}")
elif projection['status'] == 'banking':
    st.success(f"💰 {projection['message']}")
elif projection['status'] == 'exhausted':
    st.error(f"⚠️ {projection['message']}")
else:
    st.info(f"ℹ️ {projection['message']}")

# Hours remaining
st.caption(f"Driving hours until 6 PM: {hours_left:.1f}")

# 5-hour burst protection
st.divider()
st.subheader("Burst Protection")
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
st.caption(f"Week started: {week_start.strftime('%a %b %d, %-I:%M %p')} Pacific")
st.caption(f"Last updated: {latest['timestamp'].strftime('%Y-%m-%d %H:%M:%S')} UTC")
st.caption(f"Data points: {len(df)}")

# Auto-refresh
st.button("🔄 Refresh")

"""
Alpha Usage Dashboard - Streamlit app for monitoring Claude API usage.

The speedometer model:
- Instantaneous rate: How fast am I going RIGHT NOW (cools off when idle)
- Sustained rate: Average speed today (6am to now)
- Sustainable rate: Required pace to reach Reno (100%) by sundown (reset)

Blackbody spectrum for instantaneous rate:
- Below 5%/hr: Normal (dark background)
- 5%/hr: Starting to glow (2000K orange)
- 100%/hr: Max heat (10000K blue-white)
"""

import pandas as pd
import streamlit as st
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

# Config
CSV_PATH = Path("/data/quota.csv")
REFRESH_SECONDS = 2
PACIFIC = ZoneInfo("America/Los_Angeles")
UTC = ZoneInfo("UTC")

# Driving hours: 6 AM to 6 PM Pacific
DRIVING_START_HOUR = 6
DRIVING_END_HOUR = 18

# Blackbody color palette (temperature in Kelvin -> hex color)
BLACKBODY_COLORS = {
    1000: "#ff3800",   # Deep red-orange
    2000: "#ff8912",   # Orange
    3000: "#ffb46b",   # Pale orange
    4000: "#ffd2a1",   # Warm white
    5000: "#fff4ea",   # Neutral white
    6000: "#fff9fd",   # Cool white
    7000: "#f5f3ff",   # Slightly blue
    8000: "#e8ecff",   # Blue-white
    10000: "#cad8ff",  # Blue
}


def kelvin_to_hex(kelvin: float) -> str:
    """Convert temperature to blackbody color via interpolation."""
    temps = sorted(BLACKBODY_COLORS.keys())

    if kelvin <= temps[0]:
        return BLACKBODY_COLORS[temps[0]]
    if kelvin >= temps[-1]:
        return BLACKBODY_COLORS[temps[-1]]

    for i, t in enumerate(temps[:-1]):
        if temps[i] <= kelvin <= temps[i + 1]:
            t_low, t_high = temps[i], temps[i + 1]
            break

    ratio = (kelvin - t_low) / (t_high - t_low)
    c_low = BLACKBODY_COLORS[t_low]
    c_high = BLACKBODY_COLORS[t_high]

    r1, g1, b1 = int(c_low[1:3], 16), int(c_low[3:5], 16), int(c_low[5:7], 16)
    r2, g2, b2 = int(c_high[1:3], 16), int(c_high[3:5], 16), int(c_high[5:7], 16)

    r = int(r1 + (r2 - r1) * ratio)
    g = int(g1 + (g2 - g1) * ratio)
    b = int(b1 + (b2 - b1) * ratio)

    return f"#{r:02x}{g:02x}{b:02x}"


def rate_to_kelvin(rate_pct: float) -> float | None:
    """Map instantaneous rate to blackbody temperature.

    - Below 5%/hr: None (no heat effect)
    - 5%/hr → 2000K (starting to glow)
    - 100%/hr → 10000K (max heat)
    """
    if rate_pct < 5.0:
        return None

    clamped = min(rate_pct, 100.0)
    kelvin = 2000 + ((clamped - 5.0) / (100.0 - 5.0)) * (10000 - 2000)
    return kelvin


def count_driving_hours(start: datetime, end: datetime) -> float:
    """Count 6am-6pm Pacific hours between two timestamps."""
    start_pacific = start.astimezone(PACIFIC)
    end_pacific = end.astimezone(PACIFIC)

    hours = 0.0
    current = start_pacific

    while current < end_pacific:
        day_6am = current.replace(hour=DRIVING_START_HOUR, minute=0, second=0, microsecond=0)
        day_6pm = current.replace(hour=DRIVING_END_HOUR, minute=0, second=0, microsecond=0)

        if current >= day_6pm:
            next_day = current + timedelta(days=1)
            current = next_day.replace(hour=DRIVING_START_HOUR, minute=0, second=0, microsecond=0)
            continue

        if current < day_6am:
            current = day_6am

        window_end = min(end_pacific, day_6pm)
        if window_end > current:
            hours += (window_end - current).total_seconds() / 3600

        next_day = current + timedelta(days=1)
        current = next_day.replace(hour=DRIVING_START_HOUR, minute=0, second=0, microsecond=0)

    return hours


def get_today_6am() -> datetime:
    """Get 6 AM Pacific today."""
    now_pacific = datetime.now(PACIFIC)
    return now_pacific.replace(hour=DRIVING_START_HOUR, minute=0, second=0, microsecond=0)


def load_data():
    """Load and parse the quota CSV."""
    if not CSV_PATH.exists():
        return None
    df = pd.read_csv(CSV_PATH)
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    return df


def get_instantaneous_rate(df: pd.DataFrame, window_minutes: float = 5.0) -> float:
    """Calculate instantaneous rate that cools off over time."""
    if len(df) < 2:
        return 0.0

    now = datetime.now(UTC)
    window_start = now - timedelta(minutes=window_minutes)

    recent = df[df['timestamp'] >= window_start]

    if len(recent) < 1:
        return 0.0

    if len(recent) >= 2:
        usage_delta = recent.iloc[-1]['anthropic-ratelimit-unified-7d-utilization'] - recent.iloc[0]['anthropic-ratelimit-unified-7d-utilization']
        window_start_actual = recent.iloc[0]['timestamp'].to_pydatetime()
    else:
        idx = df.index.get_loc(recent.index[0])
        if idx > 0:
            prev = df.iloc[idx - 1]
            usage_delta = recent.iloc[0]['anthropic-ratelimit-unified-7d-utilization'] - prev['anthropic-ratelimit-unified-7d-utilization']
            window_start_actual = prev['timestamp'].to_pydatetime()
        else:
            return 0.0

    elapsed_hours = (now - window_start_actual).total_seconds() / 3600

    if elapsed_hours <= 0:
        return 0.0

    return usage_delta / elapsed_hours


def get_sustained_rate(df: pd.DataFrame, since: datetime) -> tuple[float, float]:
    """Calculate sustained rate since a given time."""
    recent = df[df['timestamp'] >= since]
    if len(recent) < 2:
        return 0.0, 0.0

    first = recent.iloc[0]
    last = recent.iloc[-1]

    usage_delta = last['anthropic-ratelimit-unified-7d-utilization'] - first['anthropic-ratelimit-unified-7d-utilization']

    driving_hours = count_driving_hours(
        first['timestamp'].to_pydatetime(),
        last['timestamp'].to_pydatetime()
    )

    if driving_hours <= 0:
        return 0.0, 0.0

    return usage_delta / driving_hours, driving_hours


# === Streamlit App ===

st.set_page_config(
    page_title="Alpha Usage",
    page_icon="🦆",
    layout="centered",
)

st.title("🦆 Alpha Usage Dashboard")


@st.fragment(run_every=REFRESH_SECONDS)
def live_dashboard():
    """Auto-updating dashboard fragment."""
    df = load_data()

    if df is None or len(df) == 0:
        st.warning("No data yet. Start using Claude Code through the proxy!")
        return

    # Get latest values
    latest = df.iloc[-1]
    util_7d = latest['anthropic-ratelimit-unified-7d-utilization']
    reset_timestamp = int(latest['anthropic-ratelimit-unified-7d-reset'])
    reset_dt = datetime.fromtimestamp(reset_timestamp, tz=UTC)

    now = datetime.now(UTC)
    now_pacific = now.astimezone(PACIFIC)

    # Core calculations
    budget_remaining = 1.0 - util_7d
    driving_hours_remaining = count_driving_hours(now, reset_dt)
    sustainable_rate = budget_remaining / driving_hours_remaining if driving_hours_remaining > 0 else 0

    today_6am = get_today_6am()
    today_6am_utc = today_6am.astimezone(UTC)
    sustained_rate, today_driving_hours = get_sustained_rate(df, today_6am_utc)

    instant_rate = get_instantaneous_rate(df, window_minutes=5.0)
    instant_pct = instant_rate * 100
    kelvin = rate_to_kelvin(instant_pct)

    if today_driving_hours >= 0.5 and sustained_rate > 0 and driving_hours_remaining > 0:
        projected_at_reset = util_7d + (sustained_rate * driving_hours_remaining)
    else:
        projected_at_reset = None

    # === Display ===

    # Speedometer
    st.subheader("⚡ Speedometer")
    if kelvin is None:
        st.markdown(
            f"""
            <div style="
                background-color: #1e1e1e;
                padding: 30px;
                border-radius: 15px;
                text-align: center;
                margin-bottom: 20px;
            ">
                <div style="font-size: 48px; font-weight: bold; color: #e0e0e0;">
                    {instant_pct:.2f}%/hr
                </div>
                <div style="font-size: 18px; color: #a0a0a0;">
                    Instantaneous Rate
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        blackbody_color = kelvin_to_hex(kelvin)
        st.markdown(
            f"""
            <div style="
                background-color: {blackbody_color};
                padding: 30px;
                border-radius: 15px;
                text-align: center;
                margin-bottom: 20px;
            ">
                <div style="font-size: 48px; font-weight: bold; color: #000;">
                    {instant_pct:.2f}%/hr
                </div>
                <div style="font-size: 18px; color: #333;">
                    Instantaneous Rate
                </div>
                <div style="font-size: 14px; color: #666; margin-top: 10px;">
                    {kelvin:.0f}K
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Trip computer
    st.subheader("🚗 Trip Computer")
    col1, col2, col3 = st.columns(3)

    with col1:
        st.metric(
            label="Sustained Rate",
            value=f"{sustained_rate * 100:.2f}%/hr",
            help="Today's average (6am to now)"
        )

    with col2:
        st.metric(
            label="Sustainable Rate",
            value=f"{sustainable_rate * 100:.2f}%/hr",
            help="Max pace to hit 100% at reset"
        )

    with col3:
        if sustainable_rate > 0:
            headroom = ((sustainable_rate - sustained_rate) / sustainable_rate) * 100
            if headroom >= 0:
                st.metric(
                    label="Headroom",
                    value=f"+{headroom:.0f}%",
                    help="How much faster you COULD go"
                )
            else:
                st.metric(
                    label="Over Pace",
                    value=f"{headroom:.0f}%",
                    help="How much you need to slow down"
                )

    # Status message
    if sustained_rate <= 0:
        st.info("🌅 No activity yet today")
    elif sustained_rate < sustainable_rate * 0.5:
        st.success("💨 Cruising — plenty of runway")
    elif sustained_rate < sustainable_rate * 0.9:
        st.success("🟢 On pace — looking good")
    elif sustained_rate < sustainable_rate:
        st.warning("🟡 Tight — you'll make it, but barely")
    else:
        st.error("🔴 Over pace — slow down to make Reno by sundown")

    # Budget status
    st.divider()
    st.subheader("📊 Budget Status")
    col1, col2, col3 = st.columns(3)

    with col1:
        st.metric(label="Used", value=f"{util_7d * 100:.1f}%")

    with col2:
        st.metric(label="Remaining", value=f"{budget_remaining * 100:.1f}%")

    with col3:
        st.metric(label="Driving Hours Left", value=f"{driving_hours_remaining:.1f}h")

    # Projection
    if projected_at_reset is not None:
        st.divider()
        st.subheader("🎯 Projection")
        col1, col2 = st.columns(2)

        with col1:
            st.metric(
                label="Projected at Reset",
                value=f"{projected_at_reset * 100:.1f}%",
                help="Based on sustained rate, not burst rate"
            )

        with col2:
            margin = (1.0 - projected_at_reset) * 100
            st.metric(
                label="Safety Margin",
                value=f"{margin:.1f}%",
            )

    # Footer
    st.divider()
    reset_pacific = reset_dt.astimezone(PACIFIC)
    st.caption(
        f"🏁 Reno: {reset_pacific.strftime('%a %b %d, %-I:%M %p')} Pacific | "
        f"📍 {now_pacific.strftime('%-I:%M:%S %p')} | "
        f"📈 {len(df)} points"
    )


# Run the auto-updating fragment
live_dashboard()

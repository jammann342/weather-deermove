from flask import Flask, render_template, request, jsonify
import requests
from datetime import datetime, timedelta, date
import os
import math


app = Flask(__name__)

VC_KEY = os.getenv("VC_KEY", "LWAPF8MZ886SB2SNDHWRUBEBW")
OC_KEY = os.getenv("OC_KEY", "bb1fe81e6e1745208e30255576075ebd")


# ---------- Helpers ----------

def parse_vc_time(t):
    """Convert VC '07:15:49' -> datetime(today 07:15:49)"""
    if not t:
        return None
    today = date.today().isoformat()
    return datetime.fromisoformat(f"{today} {t}")


def deg_to_cardinal(deg):
    dirs = [
        "N","NNE","NE","ENE","E","ESE","SE","SSE",
        "S","SSW","SW","WSW","W","WNW","NW","NNW"
    ]
    return dirs[round(deg / 22.5) % 16]


def pressure_trend(current, previous):
    if current > previous:
        return {"label": "Rising", "icon": "↑"}
    if current < previous:
        return {"label": "Falling", "icon": "↓"}
    return {"label": "Steady", "icon": "→"}


def format_daylight(start, end):
    seconds = int((end - start).total_seconds())
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}:{m:02d}"

def moon_phase_for_date(d):
    """
    Returns (phase_name, illumination_percent, icon)
    """
    known_new_moon = datetime(2000, 1, 6)
    days_since = (d - known_new_moon).days
    synodic_month = 29.53058867
    phase = (days_since % synodic_month) / synodic_month

    illumination = int((1 - math.cos(2 * math.pi * phase)) / 2 * 100)

    if phase < 0.03 or phase > 0.97:
        return ("New Moon", illumination, "🌑")
    elif phase < 0.22:
        return ("Waxing Crescent", illumination, "🌒")
    elif phase < 0.28:
        return ("First Quarter", illumination, "🌓")
    elif phase < 0.47:
        return ("Waxing Gibbous", illumination, "🌔")
    elif phase < 0.53:
        return ("Full Moon", illumination, "🌕")
    elif phase < 0.72:
        return ("Waning Gibbous", illumination, "🌖")
    elif phase < 0.78:
        return ("Last Quarter", illumination, "🌗")
    else:
        return ("Waning Crescent", illumination, "🌘")

def primary_condition_from_hour(h):
    """
    One headline condition for the Current card.
    Precip ALWAYS beats clouds.
    """
    preciptype = h.get("preciptype") or []
    if isinstance(preciptype, str):
        preciptype = [preciptype]

    snow = h.get("snowfall") or 0
    precip = h.get("precip") or 0
    visibility = h.get("visibility")
    cloud = h.get("cloudcover")

    # 1) Precip wins
    if "snow" in preciptype or snow > 0:
        if "rain" in preciptype:
            return "Snow & Rain"
        return "Snow"

    if "rain" in preciptype or precip > 0.01:
        return "Rain"

    # 2) Fog (low visibility)
    if visibility is not None and visibility < 1:
        return "Fog"

    # 3) Sky (derived; we do NOT use “partially cloudy”)
    if cloud is not None:
        if cloud > 75:
            return "Overcast"
        if cloud > 25:
            return "Mostly cloudy"
        return "Clear"

    # 4) Fallback
    return "Clear"

# ---------- Routes ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/weather", methods=["POST"])
def weather():
    data = request.get_json()
    zip_code = data.get("zip", "").strip()

    if not zip_code:
        return jsonify({"error": "ZIP required"}), 400

    # --- OpenCage ---
    oc = requests.get(
        "https://api.opencagedata.com/geocode/v1/json",
        params={
            "q": f"postalcode:{zip_code}, USA",
            "key": OC_KEY,
            "limit": 1
        }
    ).json()

    if not oc.get("results"):
        return jsonify({"error": "Invalid ZIP"}), 400

    lat = oc["results"][0]["geometry"]["lat"]
    lon = oc["results"][0]["geometry"]["lng"]

    # --- Visual Crossing ---
    vc = requests.get(
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/{lat},{lon}",
        params={
            "unitGroup": "us",
            "key": VC_KEY,
            "include": "current,days,hours,alerts"
        }
    ).json()
    alerts_raw = vc.get("alerts", [])

    alerts = []
    for a in alerts_raw:
        alerts.append({
            "event": a.get("event"),
            "headline": a.get("headline"),
            "description": a.get("description"),
            "onset": a.get("onset"),
            "ends": a.get("ends"),
            "severity": a.get("severity"),
            "urgency": a.get("urgency"),
            "certainty": a.get("certainty"),
            "areas": a.get("areas")
        })

    today = vc["days"][0]
   # --- STORM SUMMARY (today + overnight) ---

    storm = None
    GAP_HOURS = 2

    storm_hours = []
    snow_total = 0.0
    last_snow_index = None

    hour_blocks = today["hours"] + vc["days"][1]["hours"]

    for i, h in enumerate(hour_blocks):
        conditions = (h.get("conditions") or "").lower()

        if "snow" in conditions:
            ts = (
                f"{vc['days'][0]['datetime']}T{h['datetime']}"
                if i < len(today["hours"])
                else f"{vc['days'][1]['datetime']}T{h['datetime']}"
            )
            storm_hours.append(ts)
            last_snow_index = i
            snow_total += h.get("snow", 0) or 0
        else:
            if last_snow_index is not None and i - last_snow_index >= GAP_HOURS:
                break


    if storm_hours:
        storm = {
            "type": "Snow",
            "start": storm_hours[0],
            "end": storm_hours[-1],
            "snow": round(snow_total, 1)
        }

    yesterday = vc["days"][1] if len(vc["days"]) > 1 else today
    current = vc["currentConditions"]
    # --- Pressure ---
    pressure_now = round(current["pressure"] * 0.02953, 2)
    pressure_prev = today["hours"][0]["pressure"]
    trend = pressure_trend(current["pressure"], pressure_prev)

    # --- CURRENT (literal Visual Crossing "right now") ---
    current_raw = {
        "temp": round(current["temp"]),
        "feels": round(current.get("feelslike", current["temp"])),
        "conditions": current.get("conditions", "—"),
        "wind_speed": round(current.get("windspeed", 0)),
        "wind_gust": round(current.get("windgust", 0) or 0),
        "wind_deg": current.get("winddir", 0),
        "wind_dir": deg_to_cardinal(current.get("winddir", 0)),
        "pressure": pressure_now,
        "pressure_trend": trend   # ← THIS WAS MISSING
    }


    # --- CURRENT CONDITIONS (human-first logic) ---

    current_primary = None

    # 1) DAILY OVERRIDE (fixes Visual Crossing lag)
    daily_precip = today.get("preciptype") or []
    if isinstance(daily_precip, str):
        daily_precip = [daily_precip]

    if "snow" in daily_precip:
        current_primary = "Snow"
    elif "rain" in daily_precip:
        current_primary = "Rain"

    # 2) HOURLY CHECK (next few hours)
    if not current_primary:
        for h in today["hours"][:4]:
            c = primary_condition_from_hour(h)
            if c not in ("Clear", "Mostly cloudy", "Overcast"):
                current_primary = c
                break

    # 3) FINAL FALLBACK
    if not current_primary:
        current_primary = primary_condition_from_hour(today["hours"][0])


     # --- Sun / Light ---
    sunrise_dt = parse_vc_time(today.get("sunrise"))
    sunset_dt = parse_vc_time(today.get("sunset"))

    first_light = (sunrise_dt - timedelta(minutes=30)).strftime("%H:%M") if sunrise_dt else "—"
    last_light = (sunset_dt + timedelta(minutes=30)).strftime("%H:%M") if sunset_dt else "—"
    daylight = format_daylight(sunrise_dt, sunset_dt) if sunrise_dt and sunset_dt else "—"

    # --- NOW ---
    today_summary = {
        "temp": round(current["temp"]),
        "feels": round(current["feelslike"]),
        "conditions": current_primary,
        "high": round(today["tempmax"]),
        "low": round(today["tempmin"]),
        "wind_speed": round(current["windspeed"]),
        "wind_gust": round(current.get("windgust", 0)),
        "wind_deg": current["winddir"],
        "wind_dir": deg_to_cardinal(current["winddir"]),
        "pressure": pressure_now,
        "pressure_trend": trend,
        "sunrise": today.get("sunrise"),
        "sunset": today.get("sunset"),
        "first_light": first_light,
        "last_light": last_light,
        "daylight": daylight
    }

    # --- HOURLY (FIXED LOCATION) ---
    hourly = []
    for h in today["hours"][:24]:
        hourly.append({
            "time": h["datetime"][:5],
            "temp": round(h["temp"]),
            "conditions": h["conditions"],
            "wind_speed": round(h["windspeed"]),
            "wind_dir": deg_to_cardinal(h["winddir"]),
            "wind_deg": h["winddir"],
            "gust": round(h.get("windgust", 0)),
            "pressure": round(h["pressure"] * 0.02953, 2)
        })

    ten_day = []

    for d in vc["days"][:10]:
        day_date = datetime.strptime(d["datetime"], "%Y-%m-%d")
        moon_name, moon_pct, moon_icon = moon_phase_for_date(day_date)

        # --- Sun / Light (PER DAY) ---
        sunrise_dt = parse_vc_time(d.get("sunrise"))
        sunset_dt = parse_vc_time(d.get("sunset"))

        first_light = (
            (sunrise_dt - timedelta(minutes=30)).strftime("%H:%M")
            if sunrise_dt else "—"
        )

        last_light = (
            (sunset_dt + timedelta(minutes=30)).strftime("%H:%M")
            if sunset_dt else "—"
        )

        daylight = (
            format_daylight(sunrise_dt, sunset_dt)
            if sunrise_dt and sunset_dt else "—"
        )

        # --- HOURLY (PER DAY) ---
        day_hours = []

        for h in d.get("hours", [])[:24]:
            day_hours.append({
                "time": h["datetime"][:5],
                "temp": round(h["temp"]),
                "conditions": h["conditions"],
                "wind_speed": round(h["windspeed"]),
                "wind_dir": deg_to_cardinal(h["winddir"]),
                "wind_deg": h["winddir"],
                "gust": round(h.get("windgust", 0)),
                "pressure": round(h["pressure"] * 0.02953, 2)
            })

        ten_day.append({
            "date": d["datetime"],
            "high": round(d["tempmax"]),
            "low": round(d["tempmin"]),
            "conditions": d["conditions"],
            "wind_deg": d.get("winddir", 0),
            "wind_dir": deg_to_cardinal(d.get("winddir", 0)),

            "moon": {
                "name": moon_name,
                "illum": moon_pct,
                "icon": moon_icon
            },

            "sunrise": d.get("sunrise"),
            "sunset": d.get("sunset"),
            "first_light": first_light,
            "last_light": last_light,
            "daylight": daylight,

            "hours": day_hours
        })




    return jsonify({
        "current": current_raw,
        "today": today_summary,
        "storm": storm,
        "alerts": alerts,
        "hourly": hourly,
        "ten_day": ten_day
    })




if __name__ == "__main__":
    app.run(debug=True)

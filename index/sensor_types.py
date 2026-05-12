"""
sensor_types.py
===============
Sensor categorisation for the OpenSenseMap indexer.

Categories
----------
temperature     – air/water/soil/surface temperature
humidity        – relative and absolute humidity, dew point
pressure        – atmospheric / barometric pressure
air_quality     – particulate matter (PM1/2.5/4/10), gases (CO₂, CO, NO₂,
                  O₃, VOC, NH₃, etc.)
light           – illuminance (lux), UV intensity/index, infrared, visible
noise           – sound pressure level, dB/dBA/dBC metrics
wind            – wind speed, gusts, direction
rain            – precipitation rate, rain totals, rain events
soil            – soil moisture, soil temperature, soil conductivity
water           – water temperature, water level, water conductivity/ORP/pH
power           – voltage, current, power (W/kW), energy (Wh/kWh), battery
radiation       – ionising radiation (µSv/h, CPM, mGy)
people          – occupancy / presence / visitor counters
other           – anything that does not match above

Usage
-----
    from sensor_types import categorize_sensor

    category = categorize_sensor("Temperatur", "°C")          # → "temperature"
    category = categorize_sensor("PM2.5", "µg/m³")            # → "air_quality"
    category = categorize_sensor("Lautstärke", "dB")          # → "noise"
    category = categorize_sensor("Mystery", "Spatzen")        # → "other"
"""

from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────────────
# Keyword tables
# Each entry is (category, [title_keywords], [unit_keywords])
# Matching is case-insensitive; title/unit keywords are checked with `in`.
# Rules are evaluated top-to-bottom; first match wins.
# ──────────────────────────────────────────────────────────────────────────────

_RULES: list[tuple[str, list[str], list[str]]] = [

    # ── Particulate matter & gas pollutants (air_quality) ───────────────────
    ("air_quality", [
        "pm1", "pm2", "pm4", "pm10", "pm 1", "pm 2", "pm 4", "pm 10",
        "feinstaub", "feinstaubkonzentration", "finedust",
        "dust sensor", "dust_concentration", "dust particle",
        "partikel", "sds_p", "sds p", "mc_1", "mc_2", "mc_4", "mc_10", "nc_0",
        "no2", "no₂", "nox", "nox",
        "co2", "co₂", "co2eq", "co₂eq", "c02",   # c02 is a common typo
        "co,", "co ", " co,",                      # CO gas (not "Kosteus")
        "ozon", "ozone",
        "nh3",
        "h2,", "c2h5oh", "ch4", "c4h10", "c3h8",  # gas sensor combos
        "voc", "tvoc", "iaq", "luftqualität", "innenraumluftqualität",
        "indoor air quality", "air quality",
        "radioaktivität", "radiation",              # ionising — also caught below
        "background radiation",
    ], [
        "µg/m³", "µg/m3", "ug/m3", "µg/m^3", "µg/cm³", "μg/m³", "μg/m3",
        "μg/m^3", "pcs/0.01cf", "pcs/cf", "pcs/m³",
        "µg/m³ sds011",
        "pm2.5", "pm10",                           # unit strings like "PM2.5"
        "iaq", "co2eq", "co2-e",
        "ppm",                                     # gas concentrations
        "ppb",
        "µs/h", "usv/h", "msv/h", "cpm", "mkr/h", # radiation units
        "µsv/h",
    ]),

    # ── Temperature ─────────────────────────────────────────────────────────
    ("temperature", [
        "temperatur", "temperature", "temp",
        "wassertemperatur", "wasser temperatur",
        "bodentemperatur", "boden-temperatur", "soil temperature",
        "lufttemperatur", "luft-temperatur",
        "air temperature",
        "ostsee",                # "Ostsee" sensor is a water temp sensor
        "taupunkt", "dew point", "dew",
        "feels like",
        "wärme", "heat index",
        "cpu-temp", "cpu-temperatur", "systemtemperatur",
        "case temperature",
    ], [
        "°c", "c°", "ºc", "celsius", "celcius",
        "grad celsius", "degree celcius", "degree celsius", "°",
        "k",                                       # Kelvin (rare but present)
    ]),

    # ── Humidity ────────────────────────────────────────────────────────────
    ("humidity", [
        "luftfeuchtigkeit", "luftfeuchte", "luftfeuchtig",
        "rel. luftfeuchte", "relative luftfeuchte", "relative humidity",
        "rel. humidity", "rel. hum", "humidity", "humid",
        "humidex",
        "feuchte", "feuchtigkeit",
        "kosteus", "ilmankosteus", "ilmanpaine",   # Finnish sensor names
        "umidade", "humidade",                     # Portuguese
        "relatív páratartalom",                    # Hungarian
        "soil humidity", "soil moisture", "bodenfeuchte",
        "bodenfeuchtigkeit", "feuchtigkeit im boden",
        "plant moisture",
        "moisture sensor",
        "water level",                             # kept in humidity; water level → water below
    ], [
        "%rh", "% rh", "%rel", "% rel", "rh(%)", "rel. h. %", "rel.h.%",
        "rh", "percent",
    ]),

    # ── Atmospheric pressure ────────────────────────────────────────────────
    ("pressure", [
        "luftdruck", "druck", "pressure", "airpressure", "air pressure",
        "air preassure",
        "atmospheric pressure", "atm pressure",
        "pression", "pression atmosphérique",      # French
        "nyomás",                                  # Hungarian
        "ilmanpaine",
        "barometer",
    ], [
        "hpa", "hpa", "pa", "mbar", "millibar", "bar",
        "hectopascal", "pascal", "hektopascal",
        "hppcf",                                   # odd unit from archive
    ]),

    # ── Light / UV ──────────────────────────────────────────────────────────
    ("light", [
        "uv", "ultra", "uv-intensität", "uv intensität", "uv intensity",
        "uv-index", "uv index", "uv-säteily", "uv-strahlung", "uv strahlung",
        "uv-radiation", "uv-radioation",
        "uv-a", "uv-b", "uv-c",
        "licht", "light", "beleuchtung", "beleuchtungsstärke",
        "helligkeit", "lichtstärke", "lichtstaerke", "lichtsensor",
        "luxmeter", "uvmeter",
        "illuminance", "eclairement",              # French
        "valonmäärä", "valomäärä", "valonmäärä",  # Finnish
        "solar lux", "solar radiation",
        "dämmerung",
        "ldr",
        "infrared", "infrarot", "ir,",
        "visible light",
        "farbtemperatur",
    ], [
        "lux", "lx", "klx",
        "µw/cm²", "μw/cm²", "uw/cm²", "mw/cm²", "µw/cm2", "μw/cm2",
        "mw/m^2", "w/m^2", "w/m2",
        "uv-index", "uvi",
        "lichtpegel", "/1024",
    ]),

    # ── Noise / Sound ───────────────────────────────────────────────────────
    ("noise", [
        "schall", "lautstärke", "lärm", "geräuschpegel", "schallpegel",
        "sound", "noise", "soundpresure",
        "umgebungslautstärke",
        "laeq", "lamax", "lamin", "laf",
        "mikrofon",
        "ultra schallwellen",
    ], [
        "db", "dba", "dbc", "db(a)", "db(c)", "dbm",  # note: dbm also = wifi
        "schallpegel", "int",                          # Schall,int in archive
    ]),

    # ── Wind ────────────────────────────────────────────────────────────────
    ("wind", [
        "wind", "windgeschwindigkeit", "windböen", "windrichtung",
        "wind speed", "wind gust", "wind direction", "wind angle",
        "windspeed",
    ], [
        "bft",                                         # Beaufort
        "m/s", "km/h",
        "degrees", "grad", "°",
    ]),

    # ── Rain / Precipitation ─────────────────────────────────────────────────
    ("rain", [
        "regen", "niederschlag", "regenmänge", "regenmenge", "regenintensität",
        "rain", "precipitation", "niederschlagsmenge", "niederschlagssensor",
        "regensensor",
    ], [
        "mm/min", "mm/h", "mm/m²", "l/m²/h", "l/m2/h", "mm",
        "0/1",                                         # binary rain sensor
    ]),

    # ── Soil ────────────────────────────────────────────────────────────────
    ("soil", [
        "soil", "boden",
        "bodenfeuchte", "bodenfeuchtigkeit", "boden-feuchtigkeit",
        "bodentemperatur", "boden-temperatur",
        "leitfähigkeit", "elektrische leitfähigkeit", "boden-leitfähigkeit",
        "ph-wert", "ph wert",
        "capacitive soil",
    ], [
        "ms/cm", "µs/cm", "µs", "ph-wert",
    ]),

    # ── Water ───────────────────────────────────────────────────────────────
    ("water", [
        "wasser", "water", "wasserstand",
        "ostsee",
        "wassertemperatur", "water temperature",
        "orp", "leitfähigkeit", "trübung", "leitwert",
        "water level", "water flow", "flow rate",
        "liters total", "liter gesamt", "wassermenge",
    ], [
        "orp", "mv",
    ]),

    # ── Power / Energy / Battery ─────────────────────────────────────────────
    ("power", [
        "spannung", "eingangsspannung", "versorgungsspannung",
        "akkuspannung", "batterie", "batteriespannung", "batterieladung",
        "battery", "bat,", "bat ",
        "solar", "pvvoltage", "pvcurrent", "pvpower",
        "battery voltage", "battery charging", "battery capacity",
        "load voltage", "load current", "load power",
        "energy", "energie",
        "leistung", "tagesleistung",
        "generatedenergytoday", "consumedenergytoday",
        "totalgenerated", "totalconsumed",
        "charging",
        "powergrid", "electric mains", "netzfrequenz",
    ], [
        "v", "mv", "a", "w", "kw", "wh", "kwh", "on/off",
        "hz",
    ]),

    # ── People / Presence ───────────────────────────────────────────────────
    ("people", [
        "people", "person", "persons",
        "besucherzahl", "besucher", "besucherzähler",
        "attendance", "presence", "number of people",
        "bluetooth devices",
        "überholvorgang", "overtaking",
    ], [
        "person(s)", "persons", "people", "besucher", "anzahl",
    ]),
]


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def categorize_sensor(title: str, unit: str) -> str:
    """
    Return the category string for a sensor identified by its *title* and *unit*.

    Parameters
    ----------
    title : str
        The sensor's ``title`` field from the OpenSenseMap JSON, e.g.
        ``"Temperatur"``, ``"PM2.5"``, ``"Lautstärke"``.
    unit : str
        The sensor's ``unit`` field, e.g. ``"°C"``, ``"µg/m³"``, ``"dB"``.

    Returns
    -------
    str
        One of: ``"temperature"``, ``"humidity"``, ``"pressure"``,
        ``"air_quality"``, ``"light"``, ``"noise"``, ``"wind"``,
        ``"rain"``, ``"soil"``, ``"water"``, ``"power"``,
        ``"radiation"``, ``"people"``, ``"other"``.
    """
    t = (title or "").lower().strip()
    u = (unit  or "").lower().strip()

    for category, title_kws, unit_kws in _RULES:
        if any(kw in t for kw in title_kws):
            return category
        if any(kw in u for kw in unit_kws):
            return category

    return "other"


# ──────────────────────────────────────────────────────────────────────────────
# CLI smoke-test  (python sensor_types.py)
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import csv, sys, pathlib, collections

    csv_path = pathlib.Path(__file__).with_name("sensor_types.csv")
    if not csv_path.exists():
        print("sensor_types.csv not found — running built-in spot-checks only", file=sys.stderr)
        samples = [
            ("Temperatur",        "°C",      "temperature"),
            ("rel. Luftfeuchte",  "%",       "humidity"),
            ("Luftdruck",         "hPa",     "pressure"),
            ("PM2.5",             "µg/m³",   "air_quality"),
            ("Feinstaub",         "pcs/0.01cf", "air_quality"),
            ("Lautstärke",        "dB",      "noise"),
            ("UV-Intensität",     "µW/cm²",  "light"),
            ("Windgeschwindigkeit","m/s",    "wind"),
            ("Regen",             "mm/min",  "rain"),
            ("Bodenfeuchte",      "%",       "soil"),
            ("Wassertemperatur",  "°C",      "temperature"),
            ("Akkuspannung",      "V",       "power"),
            ("Besucherzahl",      "Personen","people"),
            ("Zeitmaschine",      "§",       "other"),
        ]
        ok = fail = 0
        for title, unit, expected in samples:
            got = categorize_sensor(title, unit)
            status = "✓" if got == expected else "✗"
            if got != expected:
                fail += 1
                print(f"  {status} ({title!r}, {unit!r}) → {got!r}  expected {expected!r}")
            else:
                ok += 1
        print(f"\n{ok} passed, {fail} failed")
        sys.exit(1 if fail else 0)

    # Full CSV audit
    counts: dict[str, int] = collections.defaultdict(int)
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            cat = categorize_sensor(row["title"], row["unit"])
            counts[cat] += 1

    print("Category distribution across sensor_types.csv:")
    for cat, n in sorted(counts.items(), key=lambda x: -x[1]):
        bar = "█" * (n // 5)
        print(f"  {cat:<15} {n:>5}  {bar}")
    print(f"  {'TOTAL':<15} {sum(counts.values()):>5}")

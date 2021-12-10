#!/usr/bin/env python3
from flask import Flask, request, make_response
from functools import lru_cache
from collections import OrderedDict
import requests
import typing
from pprint import pprint
from prometheus_client import (  # type: ignore
    Histogram,
    Counter,
    Gauge,
    CollectorRegistry,
    generate_latest,
    make_wsgi_app,
)
import pendulum  # type: ignore
import os

os.environ["TZ"] = "UTC"


request_headers = {
    "User-Agent": "github.com/grahamc graham@grahamc.com prometheus weather exporter"
}

K = typing.TypeVar("K")
V = typing.TypeVar("V")


class ExpiringCache(typing.Generic[K, V]):
    def __init__(self, cache_name: str, max_size: int, age_params: dict):
        self.entries: OrderedDict[typing.Any, typing.Any] = OrderedDict()
        self.cache_name = cache_name
        self.age_params = age_params
        self.max_size = max_size

        self.metric_cache_write = Counter(
            "expiring_dict_cache_write", "Total number of inserts.", ["cache_name"]
        )
        self.metric_cache_get = Counter(
            "expiring_dict_cache_get",
            "Count of cache hits and misses.",
            ["cache_name", "result"],
        )
        self.metric_cache_expirations = Counter(
            "expiring_dict_cache_expirations",
            "Count of cache expirations.",
            ["cache_name"],
        )
        self.metric_cache_overflow = Counter(
            "expiring_dict_cache_overflow",
            "Total count of the number of cache evictions based on max size",
            ["cache_name"],
        )
        Gauge(
            "expiring_dict_cache_size", "Maximum size of the cache", ["cache_name"]
        ).labels(cache_name).set(max_size)

    def get(self, key: K) -> typing.Optional[V]:
        record = self.entries.get(key)

        if record is None:
            self.metric_cache_get.labels(self.cache_name, "miss").inc()
            return None

        if record["validity"] < pendulum.now():
            self.metric_cache_get.labels(self.cache_name, "miss").inc()
            self.metric_cache_expirations.labels(self.cache_name).inc()
            # Delete the expired entry
            del self.entries[key]
            return None

        self.metric_cache_get.labels(self.cache_name, "hit").inc()
        return record["value"]

    def insert(self, key: K, value: V):
        if len(self.entries) >= self.max_size:
            self.metric_cache_overflow.labels(self.cache_name).inc()
            # Delete the most unused item
            self.entries.popitem(last=True)

        self.entries[key] = {
            "validity": pendulum.now().add(**self.age_params),
            "value": value,
        }
        self.entries.move_to_end(key, last=False)
        self.metric_cache_write.labels(self.cache_name).inc()


class LatLong(typing.NamedTuple):
    latitude: float
    longitude: float


class Measure(typing.NamedTuple):
    key: str
    name: str
    unit: str


class UnitConversion(typing.NamedTuple):
    name: str
    convert: typing.Callable[[float], float]


GridpointLocation = typing.NamedTuple(
    "GridpointLocation", [("wfo", str), ("x", int), ("y", int)]
)

GRIDPOINT_LOOKUPS = Histogram(
    "weather_gridpoint_lookup_seconds", "Time spent converting latlong to gridpoints"
)
GRIDPOINT_LOOKUP_FAILURES = Counter(
    "weather_gridpoint_lookup_failures", "Failed latlong conversions"
)


@lru_cache(maxsize=65536)
def get_gridpoint_location(latlong: LatLong) -> typing.Optional[GridpointLocation]:
    with GRIDPOINT_LOOKUPS.time():
        with GRIDPOINT_LOOKUP_FAILURES.count_exceptions():
            lat = latlong.latitude
            long = latlong.longitude
            data = requests.get(
                f"https://api.weather.gov/points/{lat},{long}",
                headers=request_headers,
            ).json()

    try:
        with GRIDPOINT_LOOKUP_FAILURES.count_exceptions(KeyError):
            return GridpointLocation(
                wfo=data["properties"]["cwa"],
                x=data["properties"]["gridX"],
                y=data["properties"]["gridY"],
            )
    except KeyError:
        return None


UNIT_CONVERSION_FAILURE = Counter(
    "weather_forecast_conversion_failures",
    "Failures converting forecast information",
    ["unit"],
)


def convert_unit(unit: str) -> typing.Optional[UnitConversion]:
    prom_units = {
        "degC": UnitConversion(name="celsius", convert=lambda x: x),
        "percent": UnitConversion(name="ratio", convert=lambda x: x / 100),
        "m": UnitConversion(name="meters", convert=lambda x: x),
        "degree_(angle)": UnitConversion(name="angle", convert=lambda x: x),
        "m_s-1": UnitConversion(name="meters_per_second", convert=lambda x: x),
        "mm": UnitConversion(name="meters", convert=lambda x: x / 1000),
    }

    conversion = prom_units.get(unit)
    if conversion is None:
        UNIT_CONVERSION_FAILURE.labels(unit).inc()

    return conversion


FORECAST_LOOKUPS = Histogram(
    "weather_forecast_lookup_seconds", "Time spent fetching a forecast"
)
FORECAST_LOOKUP_FAILURES = Counter(
    "weather_forecast_lookup_failures", "Exceptions fetching the forecast"
)
FORECAST_MISMATCHED_UNIT = Counter(
    "weather_forecast_mismatched_units",
    "Mismatched units in what we expect and what the API provided.",
    ["measure", "unit"],
)
FETCH_CURRENT_VALUE_FAILURE = Counter(
    "weather_forecast_fetch_current_failures",
    "Failures fetching the current measuerment",
    ["measure"],
)
FORECAST_PROCESSING_FAILURES = Counter(
    "weather_forecast_processing_failures",
    "Exceptions converting the fetched forecasts in to prometheus metrics",
)


weather_cache: ExpiringCache[GridpointLocation, str] = ExpiringCache(
    cache_name="forecasts", age_params={"minutes": 5}, max_size=1024
)


def get_weather(location: GridpointLocation):
    cached = weather_cache.get(location)

    if cached:
        return cached

    with FORECAST_LOOKUPS.time():
        with FORECAST_LOOKUP_FAILURES.count_exceptions():
            wfo = location.wfo
            x = location.x
            y = location.y
            data = requests.get(
                f"https://api.weather.gov/gridpoints/{wfo}/{x},{y}",
                headers=request_headers,
            ).json()

    measures = [
        Measure(key="temperature", name="temperature", unit="degC"),
        Measure(key="dewpoint", name="dewpoint", unit="degC"),
        Measure(key="maxTemperature", name="max_temperature", unit="degC"),
        Measure(key="minTemperature", name="min_temperature", unit="degC"),
        Measure(key="relativeHumidity", name="relative_humidity", unit="percent"),
        Measure(key="apparentTemperature", name="apparent_temperature", unit="degC"),
        Measure(key="heatIndex", name="heat_index", unit="degC"),
        Measure(key="windChill", name="wind_chill", unit="degC"),
        Measure(key="skyCover", name="sky_cover", unit="percent"),
        Measure(key="windDirection", name="wind_direction", unit="degree_(angle)"),
        Measure(key="windSpeed", name="wind_speed", unit="m_s-1"),
        Measure(key="windGust", name="wind_gust", unit="m_s-1"),
        Measure(
            key="probabilityOfPrecipitation",
            name="probability_of_precipitation",
            unit="percent",
        ),
        Measure(
            key="quantitativePrecipitation",
            name="quantitative_precipitation",
            unit="mm",
        ),
        Measure(key="iceAccumulation", name="ice_accumulation", unit="mm"),
        Measure(key="snowfallAmount", name="snowfall_mount", unit="mm"),
        Measure(key="snowLevel", name="snow_level", unit="m"),
        Measure(key="ceilingHeight", name="ceiling_height", unit="m"),
        Measure(key="visibility", name="visibility", unit="m"),
        Measure(key="transportWindSpeed", name="transport_wind_speed", unit="m_s-1"),
        Measure(
            key="transportWindDirection",
            name="transport_wind_direction",
            unit="degree_(angle)",
        ),
        Measure(key="mixingHeight", name="mixing_height", unit="m"),
        Measure(key="twentyFootWindSpeed", name="twenty_foot_wind_speed", unit="m_s-1"),
        Measure(
            key="twentyFootWindDirection",
            name="twenty_foot_wind_direction",
            unit="degree_(angle)",
        ),
    ]

    registry = CollectorRegistry()
    for measure in measures:
        try:
            with FORECAST_PROCESSING_FAILURES.count_exceptions():
                records = data["properties"][measure.key]

                uom = records.get("uom")
                if uom != f"unit:{measure.unit}":
                    FORECAST_MISMATCHED_UNIT.labels(measure.key, uom).inc()
                    pass

                value = fetch_current(records)
                if value is None:
                    FETCH_CURRENT_VALUE_FAILURE.labels(measure.key).inc()
                    pass

                unit = convert_unit(measure.unit)

                if (unit is not None) and (value is not None):
                    Gauge(
                        f"weather_{measure.name}_{unit.name}",
                        f"Weather.gov data for {measure.key} in {unit.name}",
                        registry=registry,
                    ).set(unit.convert(value))
        except KeyError as e:
            pprint(e)

    weather_cache.insert(location, registry)
    return registry


def fetch_current(data) -> typing.Optional[float]:
    now = pendulum.now()

    for report in data["values"]:
        if pendulum.parse(report["validTime"]).end > now:
            return report["value"]
    return None


app = Flask(__name__)


@app.route("/")
def root():
    resp = make_response(
        """
Export National Weather Service weather data for your network's Prometheus.

URL structure: https://weather.gsc.io/weather?latitude=...&longitude=...


Example:

  *  Berkshires, MA:    https://weather.gsc.io/weather?latitude=42.45&longitude=-73.25
  *  New York, NY:      https://weather.gsc.io/weather?latitude=40.75&longitude=-73.98
  *  San Francisco, CA: https://weather.gsc.io/weather?latitude=37.77&longitude=-122.41

Example Prometheus configuration:

    {
      "scrape_configs": [
        {
          "job_name": "weather-berkshires",
          "metrics_path": "/weather",
          "params": {
            "latitude": [
              "42.45"
            ],
            "longitude": [
              "-73.25"
            ]
          },
          "scheme": "https",
          "static_configs": [
            {
              "labels": {},
              "targets": [
                "weather.gsc.io"
              ]
            }
          ]
        }
      ]
    }



           SLA: best effort.
Poll frequency: feel free to poll every 30s or so, but the data is only
                refreshed every few minutes.
       Contact: graham@grahamc.com

""",
        200,
    )
    resp.headers["Content-Type"] = "text/plain"

    return resp


@app.route("/metrics")
def metrics():
    return make_wsgi_app()


@app.route("/weather")
def weather():
    try:
        location = LatLong(
            latitude=request.args["latitude"], longitude=request.args["longitude"]
        )
    except KeyError:
        return make_response(
            "error: latitude and longitude must be integers in the"
            " GET parameters (like: latitude=42.45&longitude=-73.25)",
            400,
        )

    gridpoint = get_gridpoint_location(location)
    if gridpoint is None:
        return make_response(
            "error: NWS can't identify a grid point location for the provided"
            " latitude and longitude. Are they formatted correctly"
            " (latitude=42.45&longitude=-73.25)? Are they in the US?",
            400,
        )

    resp = make_response(generate_latest(get_weather(gridpoint)), 200)
    resp.headers["Content-Type"] = "text/plain"

    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0")

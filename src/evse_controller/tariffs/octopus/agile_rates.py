"""Fetch and parse Agile Outgoing half-hour export rates.

The tariff and the daily energy report both need the same prices.  Keeping
the HTTP and parsing in one place stops the two from drifting apart, and the
report can still price a slot after the tariff has been stopped.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
import urllib.request

from evse_controller.utils.logging_config import error, warning


AGILE_TARIFF_CODES = {
    'A': 'AGILE-OUTGOING-19-05-13',
    'B': 'AGILE-OUTGOING-19-05-13',
    'C': 'AGILE-OUTGOING-19-05-13',
    'D': 'AGILE-OUTGOING-19-05-13',
    'E': 'AGILE-OUTGOING-19-05-13',
    'F': 'AGILE-OUTGOING-19-05-13',
    'G': 'AGILE-OUTGOING-19-05-13',
    'H': 'AGILE-OUTGOING-19-05-13',
    'J': 'AGILE-OUTGOING-19-05-13',
    'K': 'AGILE-OUTGOING-19-05-13',  # Southern Wales (default)
    'L': 'AGILE-OUTGOING-19-05-13',
    'M': 'AGILE-OUTGOING-19-05-13',
    'N': 'AGILE-OUTGOING-19-05-13',
    'P': 'AGILE-OUTGOING-19-05-13',
}

DEFAULT_AGILE_TARIFF_CODE = 'AGILE-OUTGOING-19-05-13'


def tariff_code_for_region(region: str) -> str:
    """The Agile Outgoing product code for a DNO region letter."""
    return AGILE_TARIFF_CODES.get(region, DEFAULT_AGILE_TARIFF_CODE)


def fetch_day_rates(
    region: str,
    day: date | None = None,
    tariff_code: str | None = None,
) -> list:
    """Fetch Agile Outgoing rates for *day* from the Octopus API.

    Returns a list of ``{"start", "end", "rate"}`` dicts, where the datetimes
    are local and ``rate`` is p/kWh including VAT.  An empty list means the
    fetch failed or no rates were returned; callers must treat that as
    "price unknown" rather than "free".
    """
    day = day or datetime.now().date()
    tariff_code = tariff_code or tariff_code_for_region(region)
    tariff_id = f"E-1R-{tariff_code}-{region}"
    base_url = (
        f"https://api.octopus.energy/v1/products/{tariff_code}"
        f"/electricity-tariffs/{tariff_id}"
    )

    # The API window is UTC, but the requested day is local.  Local midnight
    # is 23:00 UTC on a BST day, and a clock-change day is 23 or 25 hours
    # long, so a one-day UTC window would drop the first local hour (and part
    # of a transition day).  Ask for a day either side and filter the parsed
    # rates back to the requested local date below.
    start = datetime.combine(day - timedelta(days=1), time.min).strftime(
        '%Y-%m-%dT00:00:00Z'
    )
    end = datetime.combine(day + timedelta(days=2), time.min).strftime(
        '%Y-%m-%dT00:00:00Z'
    )
    url = (
        f"{base_url}/standard-unit-rates/"
        f"?period__started_at__gte={start}&period__started_at__lt={end}"
    )

    try:
        req = urllib.request.Request(
            url, headers={'User-Agent': 'EVSE-Controller/1.0'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())

        rates = []
        for result in data.get('results', []):
            valid_from = result.get('valid_from')
            valid_to = result.get('valid_to')
            rate_inc = result.get('value_inc_vat')

            if not all([valid_from, valid_to, rate_inc is not None]):
                continue

            start_dt = datetime.fromisoformat(valid_from.replace('Z', '+00:00'))
            end_dt = datetime.fromisoformat(valid_to.replace('Z', '+00:00'))

            # Convert UTC times to local time for correct slot index calculation.
            # This is critical on DST transition days when UTC hour != local hour.
            start_dt = start_dt.astimezone()
            end_dt = end_dt.astimezone()

            rates.append({
                'start': start_dt,
                'end': end_dt,
                'rate': round(rate_inc, 2),
            })

        rates.sort(key=lambda r: r['start'])

        # Filter to the requested day after the timezone conversion.  On DST
        # days this correctly keeps 46 or 50 local half-hours.
        unique_rates = {}
        for rate_data in rates:
            if rate_data['start'].date() != day:
                continue
            key = rate_data['start'].strftime('%Y-%m-%d %H:%M')
            if key not in unique_rates:
                unique_rates[key] = rate_data

        rates = sorted(unique_rates.values(), key=lambda r: r['start'])

        if len(rates) not in (46, 48):
            warning(
                f"AGILE_RATES: Expected 46 or 48 slots for {day}, "
                f"got {len(rates)}."
            )

        return rates
    except Exception as exc:
        error(f"AGILE_RATES: API fetch failed for {day}: {exc}")
        return []

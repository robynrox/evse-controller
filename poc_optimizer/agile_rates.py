"""
Mock Agile Outgoing Rate Fetcher

This module simulates fetching Agile Outgoing export rates from Octopus Energy API.
In production, this would call the actual Octopus API.

For the PoC, we provide realistic mock data based on typical Agile Outgoing patterns.
"""

from typing import List, Dict, Optional
from datetime import datetime, time, timedelta
from dataclasses import dataclass
import random
import time as time_module


@dataclass
class AgileRate:
    """Represents a half-hour Agile rate."""
    start: datetime
    end: datetime
    rate: float  # p/kWh
    
    def __repr__(self):
        return f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}: {self.rate:.1f}p"


class MockAgileRateFetcher:
    """
    Mock fetcher for Agile Outgoing export rates.
    
    Provides realistic rate patterns for testing without API calls.
    """
    
    # Typical Agile Outgoing patterns (p/kWh)
    PATTERNS = {
        'sunny_summer': {
            # Higher rates in evening when solar drops off
            'base': 15,
            'peak_hours': [16, 17, 18, 19],  # 16:00-20:00
            'peak_boost': 15,  # Add to base during peak
            'variability': 5,  # Random variation
        },
        'cloudy_winter': {
            # More consistent rates, less evening peak
            'base': 18,
            'peak_hours': [17, 18],
            'peak_boost': 8,
            'variability': 3,
        },
        'high_demand': {
            # High rates throughout (cold snap, low wind)
            'base': 25,
            'peak_hours': [7, 8, 17, 18, 19],
            'peak_boost': 12,
            'variability': 8,
        },
        'low_demand': {
            # Low rates (windy, mild weather)
            'base': 10,
            'peak_hours': [18, 19],
            'peak_boost': 5,
            'variability': 2,
        },
    }
    
    def __init__(self, pattern: str = 'sunny_summer'):
        """
        Initialize fetcher with a rate pattern.
        
        Args:
            pattern: One of 'sunny_summer', 'cloudy_winter', 'high_demand', 'low_demand'
        """
        if pattern not in self.PATTERNS:
            raise ValueError(f"Unknown pattern: {pattern}. Choose from {list(self.PATTERNS.keys())}")
        self.pattern = pattern
        self._seed = None
    
    def set_seed(self, seed: Optional[int]):
        """Set random seed for reproducible rates."""
        self._seed = seed
    
    def fetch_rates_for_day(self, date: datetime) -> List[AgileRate]:
        """
        Fetch rates for a specific day.
        
        Args:
            date: Date to fetch rates for
            
        Returns:
            List of 48 half-hour rates
        """
        if self._seed is not None:
            random.seed(self._seed + date.toordinal())
        
        config = self.PATTERNS[self.pattern]
        rates = []
        
        for i in range(48):
            start = datetime(date.year, date.month, date.day) + timedelta(minutes=30 * i)
            end = start + timedelta(minutes=30)
            
            hour = start.hour
            
            # Base rate with variability
            rate = config['base'] + random.uniform(-config['variability'], config['variability'])
            
            # Peak hour boost
            if hour in config['peak_hours']:
                rate += config['peak_boost']
            
            # Extra boost for 18:00-18:30 (typically highest)
            if hour == 18 and start.minute == 0:
                rate += 5
            
            rates.append(AgileRate(start=start, end=end, rate=round(rate, 1)))
        
        return rates
    
    def fetch_remaining_today(self, current_time: datetime) -> List[AgileRate]:
        """
        Fetch rates from current time until end of day.
        
        Args:
            current_time: Current datetime
            
        Returns:
            List of remaining half-hour rates
        """
        all_rates = self.fetch_rates_for_day(current_time)
        
        # Find first slot that hasn't started yet
        remaining = []
        for rate in all_rates:
            if rate.end > current_time:
                remaining.append(rate)
        
        return remaining


class RealAgileRateFetcher:
    """
    Real fetcher for Agile Outgoing export rates using Octopus API.
    
    Uses the public Octopus Energy API (no authentication required for tariff data).
    
    Note: The Agile Outgoing tariff code may change over time. Check the current
    code at: https://api.octopus.energy/v1/products/?code_contains=AGILE-OUTGOING
    
    Region Codes (for UK MPAN regions):
        _A: Eastern England
        _B: East Midlands
        _C: London
        _D: Merseyside & Northern Wales
        _E: West Midlands
        _F: North Eastern England
        _G: North Western England
        _H: Southern England
        _J: South Eastern England
        _K: Southern Wales  ← Default region
        _L: Southern Scotland
        _M: South Western England
        _N: North Wales (part)
        _P: Northern Ireland
        
        Note: Octopus uses 14 regions (A-P, excluding I and O).
        These correspond to GSP groups, with _N and _P being additional
        regions not in the standard GSP list (Wikipedia only lists A-M).
    """
    
    # Current Agile Outgoing tariff code (updated as needed)
    DEFAULT_TARIFF_CODE = "AGILE-OUTGOING-19-05-13"
    
    # Default region (Southern Wales)
    DEFAULT_REGION = "K"
    
    def __init__(self, tariff_code: str = None, region: str = None):
        """
        Initialize fetcher.
        
        Args:
            tariff_code: Agile Outgoing tariff code (default: current version)
            region: Region code (A-T, default: K for Southern Wales)
        """
        self.tariff_code = tariff_code or self.DEFAULT_TARIFF_CODE
        self.region = (region or self.DEFAULT_REGION).upper()
        
        # URL structure: /products/{tariff_code}/electricity-tariffs/{tariff_id}/
        # Tariff ID format: E-1R-{TARIFF_CODE}-{REGION}
        self.tariff_id = f"E-1R-{self.tariff_code}-{self.region}"
        self.base_url = f"https://api.octopus.energy/v1/products/{self.tariff_code}/electricity-tariffs/{self.tariff_id}"
        self._cache = {}  # Simple cache to avoid repeated requests
        self._cache_times = {}  # Track cache expiry
    
    def fetch_rates_for_day(self, date: datetime) -> List[AgileRate]:
        """
        Fetch rates for a specific day from Octopus API.
        
        Args:
            date: Date to fetch rates for
            
        Returns:
            List of half-hour rates for that day
        """
        import urllib.request
        import json
        
        # Check cache first (with 1-hour expiry)
        cache_key = date.strftime('%Y-%m-%d')
        if cache_key in self._cache:
            cache_time = self._cache_times.get(cache_key, 0)
            if time_module.time() - cache_time < 3600:  # 1 hour cache
                return self._cache[cache_key]
        
        # Build API URL
        start = date.strftime('%Y-%m-%dT00:00:00Z')
        end = (date + timedelta(days=1)).strftime('%Y-%m-%dT00:00:00Z')
        url = f"{self.base_url}/standard-unit-rates/?period__started_at__gte={start}&period__started_at__lt={end}"
        
        try:
            # Fetch data
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'EVSE-Controller-PoC/1.0'}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
            
            # Parse results
            rates = []
            for result in data.get('results', []):
                valid_from = result.get('valid_from')
                valid_to = result.get('valid_to')
                rate_inc = result.get('value_inc_vat')  # Rate in p/kWh (already in pence)
                
                if not all([valid_from, valid_to, rate_inc is not None]):
                    continue
                
                start_dt = datetime.fromisoformat(valid_from.replace('Z', '+00:00'))
                end_dt = datetime.fromisoformat(valid_to.replace('Z', '+00:00'))
                
                rates.append(AgileRate(
                    start=start_dt,
                    end=end_dt,
                    rate=round(rate_inc, 2)
                ))
            
            # Cache the results
            self._cache[cache_key] = rates
            self._cache_times[cache_key] = time_module.time()
            
            # Sort by start time (API may return in reverse order)
            rates.sort(key=lambda r: r.start)
            
            # Filter to only the requested day
            day_start = datetime(date.year, date.month, date.day, tzinfo=rates[0].start.tzinfo) if rates[0].start.tzinfo else datetime(date.year, date.month, date.day)
            day_end = day_start + timedelta(days=1)
            rates = [r for r in rates if day_start <= r.start < day_end]
            
            # Remove duplicates - keep the last (most recent) rate for each time slot
            # The API returns both day-ahead and balancing market rates
            unique_rates = {}
            for r in rates:
                key = (r.start, r.end)
                unique_rates[key] = r  # Last one wins (most recent)
            
            # Return sorted unique rates
            result = sorted(unique_rates.values(), key=lambda r: r.start)
            return result
            
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"Error: Tariff {self.tariff_code} not found. Check tariff code.")
            else:
                print(f"HTTP Error {e.code}: {e.reason}")
            return []
        except urllib.error.URLError as e:
            print(f"Network error: {e.reason}")
            return []
        except Exception as e:
            print(f"Error fetching Agile rates: {e}")
            return []
    
    def fetch_remaining_today(self, current_time: datetime) -> List[AgileRate]:
        """
        Fetch rates from current time until end of day.
        
        Args:
            current_time: Current datetime
            
        Returns:
            List of remaining half-hour rates
        """
        all_rates = self.fetch_rates_for_day(current_time)
        
        # Filter to only future slots
        remaining = []
        for rate in all_rates:
            # Handle timezone comparison
            rate_start = rate.start
            if current_time.tzinfo is not None and rate_start.tzinfo is None:
                rate_start = rate_start.replace(tzinfo=current_time.tzinfo)
            
            if rate_start > current_time:
                remaining.append(rate)
        
        return remaining
    
    def fetch_tomorrow(self) -> List[AgileRate]:
        """
        Fetch rates for tomorrow.
        
        Returns:
            List of rates for tomorrow
        """
        tomorrow = datetime.now() + timedelta(days=1)
        tomorrow = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.fetch_rates_for_day(tomorrow)
    
    def clear_cache(self):
        """Clear the rate cache."""
        self._cache.clear()
        self._cache_times.clear()


if __name__ == "__main__":
    # Demo: Show rate patterns
    print("Agile Outgoing Rate Patterns (Mock Data)")
    print("=" * 60)
    
    for pattern_name in ['sunny_summer', 'cloudy_winter', 'high_demand', 'low_demand']:
        fetcher = MockAgileRateFetcher(pattern=pattern_name)
        fetcher.set_seed(42)  # Reproducible
        
        test_date = datetime(2025, 6, 15)
        rates = fetcher.fetch_rates_for_day(test_date)
        
        print(f"\n{pattern_name.upper()}:")
        print("-" * 40)
        
        # Show afternoon/evening rates (14:00-20:00)
        afternoon = [r for r in rates if 14 <= r.start.hour < 20]
        for rate in afternoon:
            marker = " ← PEAK" if rate.rate >= 25 else ""
            print(f"  {rate}{marker}")
        
        # Statistics
        all_rates = [r.rate for r in rates]
        print(f"\n  Stats: min={min(all_rates):.1f}p, max={max(all_rates):.1f}p, avg={sum(all_rates)/len(all_rates):.1f}p")
    
    # Demo: Real API fetch
    print("\n" + "=" * 60)
    print("REAL API FETCH (Agile Outgoing)")
    print("=" * 60)
    
    try:
        real_fetcher = RealAgileRateFetcher()
        today = datetime.now()
        
        print(f"\nFetching rates for {today.strftime('%Y-%m-%d')}...")
        rates = real_fetcher.fetch_rates_for_day(today)
        
        if rates:
            print(f"Successfully fetched {len(rates)} rates")
            
            # Show afternoon/evening rates
            afternoon = [r for r in rates if 14 <= r.start.hour < 20]
            if afternoon:
                print(f"\nAfternoon/Evening rates:")
                for rate in afternoon:
                    print(f"  {rate.start.strftime('%H:%M')}-{rate.end.strftime('%H:%M')}: {rate.rate:.1f}p")
            
            # Statistics
            all_rates = [r.rate for r in rates]
            print(f"\n  Stats: min={min(all_rates):.1f}p, max={max(all_rates):.1f}p, avg={sum(all_rates)/len(all_rates):.1f}p")
        else:
            print("No rates available (rates may not be published yet)")
            
    except Exception as e:
        print(f"Error: {e}")

# Region Support for Agile Outgoing

## Overview

Agile Outgoing export rates vary by UK region due to different distribution costs and local market conditions. The PoC now supports all UK regions.

## Default Region: Southern Wales (_K)

```python
from agile_rates import RealAgileRateFetcher
from datetime import datetime

# Default is Southern Wales (_K)
fetcher = RealAgileRateFetcher()

# Or explicitly specify for user's region
fetcher = RealAgileRateFetcher(region='K')  # Southern Wales
```

## Today's Rates for Southern Wales

**Date**: 2026-03-05  
**Region**: _K (Southern Wales)  
**Total slots**: 48 (half-hourly)

### Peak Export Period (16:00-19:00)

| Time | Rate (p/kWh) |
|------|--------------|
| 16:00-16:30 | 18.3p |
| 16:30-17:00 | 19.2p |
| 17:00-17:30 | 19.3p |
| 17:30-18:00 | 19.5p ← Peak |
| 18:00-18:30 | 19.3p |
| 18:30-19:00 | 19.3p |

### Daily Statistics

| Metric | Value |
|--------|-------|
| Minimum | 9.4p/kWh |
| Maximum | 19.5p/kWh |
| Average | 11.8p/kWh |
| Peak window avg | 19.2p/kWh |
| Off-peak avg | 10.5p/kWh |

**Peak premium**: +83% vs off-peak

## All UK Regions

Octopus Agile Outgoing uses **14 regions** (A-P, excluding I and O):

| Code | Region |
|------|--------|
| A | Eastern England |
| B | East Midlands |
| C | London |
| D | Merseyside & Northern Wales |
| E | West Midlands |
| F | North Eastern England |
| G | North Western England |
| H | Southern England |
| J | South Eastern England |
| K | **Southern Wales** ← Default region |
| L | Southern Scotland |
| M | South Western England |
| N | North Wales (part) |
| P | Northern Ireland |

**Note**: Regions _A through _M correspond to GSP (Grid Supply Point) groups listed on [Wikipedia](https://en.wikipedia.org/wiki/Meter_Point_Administration_Number#Distributor_ID). Regions _N and _P are Octopus-specific additions:
- **_N**: North Wales (part) - subdivision of GSP group D
- **_P**: Northern Ireland - separate grid from Great Britain

## Usage Examples

### Compare Regions

```python
from agile_rates import RealAgileRateFetcher
from datetime import datetime

today = datetime.now()

# Compare peak rates across regions
regions = {
    'K': 'Southern Wales',
    'C': 'London',
    'R': 'Central Scotland'
}

for code, name in regions.items():
    fetcher = RealAgileRateFetcher(region=code)
    rates = fetcher.fetch_rates_for_day(today)
    peak = max(r.rate for r in rates)
    avg = sum(r.rate for r in rates) / len(rates)
    print(f"{name:20s}: Peak={peak:5.1f}p, Avg={avg:5.1f}p")
```

### Optimization with Regional Rates

```python
from agile_rates import RealAgileRateFetcher
from dp_optimizer import ExportOptimizer, ExportSlot

# Fetch rates for your region
fetcher = RealAgileRateFetcher(region='K')  # Southern Wales
rates = fetcher.fetch_rates_for_day(datetime.now())

# Convert to export slots
slots = [
    ExportSlot(
        start_time=r.start.time(),
        end_time=r.end.time(),
        rate=r.rate,
        max_energy=4.0  # 4% per 30min at 8%/hour
    )
    for r in rates
]

# Optimize export
optimizer = ExportOptimizer()
result = optimizer.optimize(slots, available_capacity=10.0)

print(f"Optimal revenue: {result.total_revenue:.2f}p")
print(f"Allocation:")
for slot, energy in zip(result.slots, result.allocation):
    if energy > 0:
        print(f"  {slot.start_time.strftime('%H:%M')}: {energy:.1f}% @ {slot.rate:.1f}p")
```

## Regional Variation Analysis

Rates vary by region due to:
1. **Distribution costs**: Different grid infrastructure costs
2. **Local generation**: Areas with more solar/wind may have lower rates
3. **Demand patterns**: Industrial vs residential mix
4. **Grid constraints**: Transmission capacity limitations

### Example Variation (Typical Day)

| Region | Peak Rate | Off-Peak | Spread |
|--------|-----------|----------|--------|
| London (C) | 22.5p | 11.2p | 11.3p |
| Southern Wales (K) | 19.5p | 9.4p | 10.1p |
| Central Scotland (R) | 18.2p | 8.9p | 9.3p |

**Implication**: Export optimization is most valuable in high-spread regions like London.

## Configuration

### For PoC Testing

```python
# Use your region
fetcher = RealAgileRateFetcher(region='K')

# Test with different region
fetcher = RealAgileRateFetcher(region='C')  # London
```

### For Main Codebase Integration

Add to `config.yaml`:

```yaml
octopus:
  export_tariff: "AGILE-OUTGOING-19-05-13"
  region: "K"  # Southern Wales
  
optimization:
  enabled: true
  # ... other parameters
```

## Finding the User's Region

The user's region is determined by their MPAN (Meter Point Administration Number):

1. Look at the user's electricity bill
2. Find the MPAN (13-digit number, usually in the format `XX XXXXXX XXX XXX`)
3. The first two digits indicate the GSP group:
   - **10-11**: Eastern England → Region _A
   - **12**: East Midlands → Region _B
   - **13**: London → Region _C
   - **14**: Merseyside & Northern Wales → Region _D or _N
   - **15**: West Midlands → Region _E
   - **16**: North Eastern England → Region _F
   - **17**: North Western England → Region _G
   - **18**: Southern England → Region _H
   - **19**: South Eastern England → Region _J
   - **20**: Southern Wales → Region _K ← Default
   - **21**: Southern Scotland → Region _L
   - **22**: South Western England → Region _M
   - **23**: North Wales (part) → Region _N
   - **24**: Northern Ireland → Region _P

**Note**: Octopus uses 14 regions (_A through _P, excluding I and O). The Wikipedia GSP list only shows A-M because _N and _P are Octopus-specific subdivisions. Northern Ireland (_P) has a separate grid from Great Britain.

**Reference**: See the [Wikipedia article on MPAN Distributor IDs](https://en.wikipedia.org/wiki/Meter_Point_Administration_Number#Distributor_ID) for the GSP group list.

## API Implementation Details

### URL Structure

```
GET https://api.octopus.energy/v1/products/{TARIFF_CODE}/
    electricity-tariffs/E-1R-{TARIFF_CODE}-{REGION}/
    standard-unit-rates/
    ?period__started_at__gte={START_DATE}
    &period__started_at__lt={END_DATE}
```

### Example for Southern Wales

```
GET https://api.octopus.energy/v1/products/AGILE-OUTGOING-19-05-13/
    electricity-tariffs/E-1R-AGILE-OUTGOING-19-05-13-K/
    standard-unit-rates/
    ?period__started_at__gte=2026-03-05T00:00:00Z
    &period__started_at__lt=2026-03-06T00:00:00Z
```

### Response Handling

The API returns:
- Multiple days of data (filtered to requested day)
- Both day-ahead and balancing market rates (deduplicated)
- Rates in p/kWh (inclusive of VAT)
- Timestamps in UTC (converted to local time)

## Testing

### Verify the User's Region

```bash
cd poc_optimizer

# Fetch rates for Southern Wales
python -c "
from agile_rates import RealAgileRateFetcher
from datetime import datetime

fetcher = RealAgileRateFetcher(region='K')
rates = fetcher.fetch_rates_for_day(datetime.now())

print(f'Southern Wales: {len(rates)} rates')
print(f'Peak: {max(r.rate for r in rates):.1f}p/kWh')
"
```

### Compare with Your Bill

1. Check your export payments on your Octopus account
2. Compare the rates you received with the API data
3. Verify they match (should be identical)

## Next Steps

1. **Verify region**: Confirm _K is correct for your MPAN
2. **Test optimization**: Run optimizer with your regional rates
3. **Compare revenue**: Check if optimized strategy beats actual export
4. **Plan integration**: Add region config to main codebase

## Questions?

- **Wrong region?** Check your MPAN prefix
- **Rates don't match?** Octopus may use different tariff code
- **Missing data?** API may not have published yet (usually by 4pm)

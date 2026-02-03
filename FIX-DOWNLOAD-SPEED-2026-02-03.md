# Download Speed "0 B/s" Fix - 2026-02-03

## Issue
Download speed displayed "0 B/s" on the first API request after service start, even though the tip sync speed ("2.4 b/s") was displayed correctly.

## Root Cause
The `get_btc_network_speed()` function calculates speed by comparing two measurements:
- **First call**: Establishes baseline (timestamp + byte counts)
- **Second call**: Calculates speed = (bytes_now - bytes_prev) / (time_now - time_prev)

Without a baseline measurement, the first API request always returned:
```python
{'btc_download_speed': 0, 'btc_upload_speed': 0}
```

This formatted to "0 B/s" on the frontend.

The `warmup_cache()` function didn't call `get_btc_network_speed()`, so the baseline was only established on the first user request, meaning the first display always showed "0 B/s".

## Solution
Added `get_btc_network_speed()` to the warmup routine with two calls:

```python
# Initialize network speed tracking (needs 2 calls to calculate speed)
get_btc_network_speed()  # First call establishes baseline
time.sleep(3)  # Wait for some data transfer
get_btc_network_speed()  # Second call calculates initial speed
```

**Location**: bitcoin-dashboard.py:2699-2706

## Result
✅ First API request now shows actual speed (e.g., "7.2 MB/s")
✅ No more "0 B/s" on initial page load
✅ Consistent display from service start

## Testing
```bash
# Test on port 8891
python3 bitcoin-dashboard.py --host localhost --port 8891 &
sleep 8
curl -s http://localhost:8891/api/stats | jq '.btc_download_speed_human'
# Result: "7.2 MB/s" ✅ (not "0 B/s")
```

## Deployment
To apply this fix to production:

```bash
# Option 1: Restart service (requires sudo)
sudo systemctl restart bitcoin-dashboard

# Option 2: Reboot machine
sudo reboot
```

After restart, the download speed will display correctly from the first page load.

## Technical Details

### Why 3-second delay?
- Gives Bitcoin Core time to transfer some data from peers
- Ensures there's a measurable byte difference between the two measurements
- Short enough to not significantly delay service startup

### Alternative Solutions Considered
1. **Return cached value on first call**: Would still show "0 B/s" initially
2. **Don't display until second call**: Would show "-" instead, confusing users
3. **Estimate based on connection count**: Inaccurate and complex

**Chosen solution**: Proactive warmup - simple, accurate, minimal startup delay

## Files Modified
- `bitcoin-dashboard.py` - Added network speed warmup (lines 2699-2706)

## Related Issues
- Initial connection/data loss fixes: FIXES-2026-02-03.md
- Async RPC optimization: ASYNC-OPTIMIZATION.md

# Filament Usage Tracking via tray_now

## Problem Solved

The original multi-filament tracking captured ALL loaded filaments in the AMS, but couldn't distinguish which ones were **actually used** during a print. For example, if you had 8 filaments loaded but only used 2 for a specific print, all 8 were marked the same way.

## Solution: Using tray_now MQTT Field

The Bambu Lab printer publishes a `tray_now` field in its MQTT payload that indicates which AMS tray is **currently active** during printing. We now use this to mark filaments as "used" vs "just loaded".

## Database Changes

### Migration File: `add_was_used_to_job_filaments.sql`

Adds a `was_used` column to track which filaments were actively consumed:

```sql
ALTER TABLE printer_job_filaments
    ADD COLUMN IF NOT EXISTS was_used BOOLEAN DEFAULT false;
```

## How tray_now Works

The `tray_now` field is an integer that encodes the AMS unit and tray position:

- **Formula**: `tray_now = (ams_id × 4) + tray_id`
- **Example**: `tray_now = 5` means AMS 1, Tray 1 (because 5 = 1×4 + 1)
- **Special Values**:
  - `255` or `254` = External spool (no AMS)
  - `0-15` = Valid AMS positions (4 AMS units × 4 trays each)

### Decoding Example

```
tray_now = 0  → AMS 0, Tray 0
tray_now = 3  → AMS 0, Tray 3
tray_now = 4  → AMS 1, Tray 0
tray_now = 7  → AMS 1, Tray 3
tray_now = 15 → AMS 3, Tray 3
```

## Code Changes

### 1. Extract tray_now from MQTT Data

Updated `get_status_safe()` in [monitor_printers.py](../../examples/monitor_printers.py):

```python
# Get raw print data for tray_now field
raw_data = self.client.mqtt_client.dump()
print_data = raw_data.get('print', {})

result = {
    ...
    'tray_now': print_data.get('tray_now'),  # Active tray ID
    'tray_tar': print_data.get('tray_tar')   # Target tray ID
}
```

### 2. Decode tray_now to Identify Active Filament

Updated `_log_job_filaments()`:

```python
tray_now = status_data.get('tray_now')
active_ams_id = None
active_tray_id = None

if tray_now is not None:
    tray_now_int = int(tray_now) if isinstance(tray_now, str) else tray_now
    if tray_now_int < 16:  # Valid AMS tray
        active_ams_id = tray_now_int // 4  # Integer division
        active_tray_id = tray_now_int % 4   # Modulo
```

### 3. Mark Filaments as Used

During filament capture, check if each filament matches the active position:

```python
was_used = (ams_id == active_ams_id and tray_id == active_tray_id)

INSERT INTO printer_job_filaments (
    ..., was_used, ...
) VALUES (
    ..., %s, ...
)
```

## Data Captured

For each print job, the system now records:

| Column | Purpose | Example |
|--------|---------|---------|
| `ams_id` | AMS unit number | 0, 1, 2, 3, or NULL |
| `tray_id` | Tray position | 0, 1, 2, 3, or NULL |
| `is_primary` | Initially active at job start | true/false |
| `was_used` | **Actually consumed during print** | **true/false** |
| `filament_color` | Color hex code | "FF5733" |
| `filament_type` | Material type | "PLA", "PETG" |

## Example Queries

### See Only Filaments Actually Used

```sql
SELECT
    pjh.id,
    pjh.filename,
    pjh.start_time,
    pjf.filament_type,
    pjf.filament_color,
    pjf.ams_id,
    pjf.tray_id
FROM printer_job_history pjh
JOIN printer_job_filaments pjf ON pjh.id = pjf.job_history_id
WHERE pjf.was_used = true
ORDER BY pjh.start_time DESC;
```

### Count Prints by Color (Only Actually Used)

```sql
SELECT
    pjf.filament_color,
    pjf.filament_type,
    COUNT(*) as print_count
FROM printer_job_history pjh
JOIN printer_job_filaments pjf ON pjh.id = pjf.job_history_id
WHERE pjh.filename LIKE '%book_tracker%'
    AND pjh.status = 'FINISH'
    AND pjf.was_used = true  -- Only count filaments actually used
GROUP BY pjf.filament_color, pjf.filament_type
ORDER BY print_count DESC;
```

### Compare Loaded vs Used Filaments

```sql
SELECT
    pjh.id as job_id,
    pjh.filename,
    COUNT(*) FILTER (WHERE pjf.was_used = false) as loaded_but_unused,
    COUNT(*) FILTER (WHERE pjf.was_used = true) as actually_used
FROM printer_job_history pjh
JOIN printer_job_filaments pjf ON pjh.id = pjf.job_history_id
WHERE pjh.status = 'FINISH'
GROUP BY pjh.id, pjh.filename
ORDER BY pjh.start_time DESC;
```

### Multi-Color Print Analysis

```sql
-- Find prints that used multiple colors
SELECT
    pjh.id,
    pjh.filename,
    pjh.start_time,
    STRING_AGG(pjf.filament_color, ', ' ORDER BY pjf.ams_id, pjf.tray_id) as colors_used,
    COUNT(*) FILTER (WHERE pjf.was_used = true) as num_colors
FROM printer_job_history pjh
JOIN printer_job_filaments pjf ON pjh.id = pjf.job_history_id
WHERE pjf.was_used = true
GROUP BY pjh.id, pjh.filename, pjh.start_time
HAVING COUNT(*) FILTER (WHERE pjf.was_used = true) > 1
ORDER BY num_colors DESC, pjh.start_time DESC;
```

## Testing

After applying the database migration:

1. **Apply Migration:**
   ```bash
   psql -h your_host -U your_user -d your_db -f database/migrations/add_was_used_to_job_filaments.sql
   ```

2. **Restart Monitor:**
   ```bash
   python examples/monitor_printers.py
   ```

3. **Start a Print Job** and observe console output:
   ```
   [green]Filaments loaded (8):[/] PLA (FF0000), PLA (00FF00), PLA (0000FF), ...
   [cyan]Filaments actively used (1):[/] PLA (FF0000)
   ```

4. **Query the Database:**
   ```sql
   SELECT * FROM printer_job_filaments WHERE job_history_id = <your_job_id>;
   ```

   You should see:
   - Multiple rows with `was_used = false` (loaded but not used)
   - One or more rows with `was_used = true` (actually consumed)

## Benefits

1. **Accurate Color Tracking**: Know exactly which color was used, not just which were loaded
2. **Multi-Color Support**: For multi-color prints, track all filaments actually consumed
3. **Inventory Accuracy**: Only deduct from stock for filaments actually used
4. **Cost Analysis**: Calculate real material costs based on actual usage
5. **Production Planning**: See which color combinations are most popular

## Troubleshooting

### tray_now is Always NULL

- Check that your printer firmware supports this field (newer firmware versions)
- Verify the MQTT connection is receiving complete data
- Look at raw MQTT payload: `printer.mqtt_client.dump()`

### All Filaments Marked as Not Used

- Ensure `tray_now` is being captured at job start (not during idle)
- Check that the decoding formula matches your printer's encoding
- Some printers may use different encoding schemes

### External Spool Not Marked as Used

- External spools should always be marked `was_used = true` when active
- Check that `tray_now` returns 255 or 254 for external spool

# Filament Capture Implementation

This document describes the filament capture feature that tracks which filament/color is used for each print job.

## Problem Solved

When printing the same G-code file with different filament colors (e.g., a book tracker in 5 different colors), the system previously only tracked the filename. Now it captures exactly which filament was used for each print.

## Database Changes Required

### 1. Create `printer_job_filaments` Table

```sql
CREATE TABLE printer_job_filaments (
    id SERIAL PRIMARY KEY,
    job_history_id INTEGER NOT NULL REFERENCES printer_job_history(id) ON DELETE CASCADE,
    printer_id INTEGER NOT NULL REFERENCES printers(printer_id),

    -- Filament identification
    filament_id VARCHAR(255),
    tray_uuid VARCHAR(255),

    -- AMS/Tray location (optional, for future enhancement)
    ams_id INTEGER,
    tray_id INTEGER,

    -- Filament details (snapshot at job start)
    filament_name VARCHAR(255),
    filament_type VARCHAR(100),
    filament_color VARCHAR(20),
    filament_vendor VARCHAR(255),

    -- Temperature settings
    temp_min INTEGER,
    temp_max INTEGER,
    bed_temp INTEGER,

    -- Material properties
    weight VARCHAR(50),
    cost NUMERIC(10, 2),
    density NUMERIC(10, 4),
    diameter NUMERIC(5, 3),

    -- Metadata
    captured_at TIMESTAMP DEFAULT NOW(),

    CONSTRAINT unique_job_filament UNIQUE(job_history_id)
);

CREATE INDEX idx_job_filaments_job_id ON printer_job_filaments(job_history_id);
CREATE INDEX idx_job_filaments_printer_id ON printer_job_filaments(printer_id);
CREATE INDEX idx_job_filaments_color ON printer_job_filaments(filament_color);
```

### 2. Update the Stock Transaction Trigger

Apply the SQL from `update_stock_trigger_with_filament_info.sql` to enhance stock transaction notes with filament details.

```bash
psql -h your_host -U your_user -d your_db -f update_stock_trigger_with_filament_info.sql
```

## Code Changes Made

### monitor_printers.py

Three new methods were added to the `SafePrinterMonitor` class:

1. **`_extract_filament_info(status_data)`** (lines 598-685)
   - Extracts filament information from vt_tray data
   - Removes 'FF' suffix from 8-character hex colors
   - Looks up additional details from bambu_filament_profiles table
   - Returns a dictionary with all filament properties

2. **`_log_job_filament(job_id, printer_id, filament_info)`** (lines 687-735)
   - Saves filament information to printer_job_filaments table
   - Uses ON CONFLICT to prevent duplicates
   - Displays confirmation message

3. **`_log_job_start(...)` - Enhanced** (lines 737-785)
   - Now accepts status_data parameter
   - Extracts and logs filament info after creating job record

## How It Works

1. **Job Start Detection**: Monitor detects a print job starting (status changes to RUNNING)
2. **Filament Extraction**: System reads the active filament data from the printer's vt_tray
3. **Database Enrichment**: Looks up additional filament properties from bambu_filament_profiles
4. **Storage**: Saves complete filament snapshot to printer_job_filaments table
5. **Job Completion**: When job finishes, the trigger includes filament info in stock transaction notes

## Data Captured

For each print job, the system captures:
- **Filament Type**: PLA, PETG, TPU, etc.
- **Color**: Hex color code (e.g., "FF5733")
- **Vendor**: Manufacturer name (e.g., "Polymaker", "eSun")
- **Temperatures**: Nozzle min/max, bed temperature
- **Material Properties**: Cost, density, diameter
- **Identifiers**: tray_uuid, filament_id for tracking

## Example Queries

### See All Prints with Colors
```sql
SELECT
    pjh.id,
    pjh.filename,
    pjh.start_time,
    pjf.filament_type,
    pjf.filament_color,
    pjf.filament_vendor
FROM printer_job_history pjh
LEFT JOIN printer_job_filaments pjf ON pjh.id = pjf.job_history_id
WHERE pjh.status = 'FINISH'
ORDER BY pjh.start_time DESC;
```

### Count Prints by Color for Specific Item
```sql
SELECT
    pjf.filament_color,
    pjf.filament_type,
    COUNT(*) as print_count
FROM printer_job_history pjh
JOIN printer_job_filaments pjf ON pjh.id = pjf.job_history_id
WHERE pjh.filename LIKE '%book_tracker%'
    AND pjh.status = 'FINISH'
GROUP BY pjf.filament_color, pjf.filament_type
ORDER BY print_count DESC;
```

### View Stock Transactions with Filament Details
```sql
SELECT
    st.id,
    st.transaction_date,
    st.item_id,
    st.quantity,
    st.notes,
    pjf.filament_type,
    pjf.filament_color,
    pjf.filament_vendor
FROM stock_transactions st
JOIN printer_job_filaments pjf ON st.print_job_id = pjf.job_history_id
WHERE st.transaction_type = 'PRINT_COMPLETE'
ORDER BY st.transaction_date DESC;
```

### Cost Analysis by Color
```sql
SELECT
    pjf.filament_color,
    pjf.filament_vendor,
    COUNT(*) as prints,
    AVG(pjf.cost) as avg_cost_per_kg
FROM printer_job_filaments pjf
JOIN printer_job_history pjh ON pjf.job_history_id = pjh.id
WHERE pjh.status = 'FINISH'
    AND pjf.cost IS NOT NULL
GROUP BY pjf.filament_color, pjf.filament_vendor
ORDER BY prints DESC;
```

## Testing

After applying the database changes:

1. Restart the monitor script
2. Start a print job
3. Check the console output for "Filament captured: [details]"
4. Query the printer_job_filaments table to verify data was saved
5. Complete the print and verify the stock transaction notes include filament info

## Troubleshooting

### No Filament Data Captured
- Check that vt_tray data is available (some printers may not support this)
- Verify the printer has an active filament loaded
- Look at monitor logs for any errors in `_extract_filament_info`

### Missing Database Info
- Ensure bambu_filament_profiles table is populated
- Check that filament_id from printer matches records in the table

### Stock Notes Don't Show Filament
- Verify the trigger function was updated
- Check that printer_job_filaments table has data for the job
- Look for errors in database logs during trigger execution

## Benefits

1. **Color Variation Tracking**: Know exactly which color was printed when
2. **Inventory Accuracy**: Track filament usage by specific spool
3. **Cost Analysis**: Calculate actual material costs per print/color
4. **Quality Tracking**: Trace quality issues back to specific filament batches
5. **Production Planning**: See which colors are most popular

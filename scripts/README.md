# Bambu Lab Filament Profile Scripts

This directory contains scripts to fetch and store comprehensive filament information from the BambuStudio GitHub repository.

## Files

- **`fetch_bambu_filaments.py`** - Main script that fetches and parses filament profiles
- **`create_bambu_filament_table.sql`** - SQL script to create the database table
- **`run_bambu_fetcher.py`** - Simple runner script with user-friendly interface

## Setup

### 1. Install Dependencies

```bash
pip install requests psycopg2 rich python-dotenv
```

### 2. Create Database Table

Execute the SQL script in your PostgreSQL database:

```bash
psql -h your_host -d your_database -U your_user -f scripts/create_bambu_filament_table.sql
```

Or copy the contents of `create_bambu_filament_table.sql` and run it in your database client.

### 3. Environment Variables

Make sure your `.env` file contains the database configuration (same as used in `monitor_printers.py`):

```env
DB_HOST=your_database_host
DB_PORT=5432
DB_NAME=your_database_name
DB_USER=your_database_user
DB_PASSWORD=your_database_password
```

## Usage

### Option 1: Simple Runner (Recommended)

```bash
python scripts/run_bambu_fetcher.py
```

This provides an interactive interface that will:
1. Fetch all filament profiles from GitHub
2. Display a summary
3. Ask what you want to do with the data (save to database, JSON, or both)

### Option 2: Direct Import

```python
from scripts.fetch_bambu_filaments import BambuFilamentFetcher

fetcher = BambuFilamentFetcher()
filaments = fetcher.fetch_all_filaments()
fetcher.display_summary()
fetcher.save_to_database()
```

## What It Does

The script fetches all `*@base.json` files from:
https://github.com/bambulab/BambuStudio/tree/master/resources/profiles/BBL/filament

For each filament profile, it extracts:

### Basic Information
- **Filament ID** (e.g., "GFA01", "GFB00") - Used for matching with printer data
- **Name** (e.g., "Bambu PLA Sparkle @base")
- **Vendor** (e.g., "Bambu Lab")
- **Material Type** (PLA, ABS, PETG, TPU, etc.)

### Temperature Settings
- **Nozzle Temperature Range** (min/max in Celsius)
- **Bed Temperature** (recommended and initial layer)

### Material Properties
- **Density** (g/cm³)
- **Cost** (per kg)
- **Flow Ratio** (printing flow adjustment)
- **Impact Strength** (material strength rating)
- **Diameter** (typically 1.75mm)

### Print Settings
- **Retraction Length/Speed**
- **Print Speed** recommendations
- **Start/End G-code** (filament-specific commands)

### Raw Data
- **Complete JSON** stored for reference and future parsing

## Database Schema

The `bambu_filament_profiles` table includes:
- Indexed fields for fast lookups (`filament_id`, `material_type`, `vendor`)
- JSONB field for complete raw data
- Automatic timestamp tracking
- Unique constraint on `filament_id`

## Integration with Monitor Script

The fetched data can be used to enhance the filament recognition in `monitor_printers.py`:

1. **Enhanced Matching**: Use the database to look up detailed filament information
2. **Temperature Validation**: Compare printer temperatures with recommended ranges
3. **Cost Tracking**: Calculate material costs for print jobs
4. **Better Display**: Show full filament names and specifications

## Example Output

```
Bambu Lab Filament Profiles Summary
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ Filament ID ┃ Name                           ┃ Material  ┃ Vendor    ┃ Temp Range  ┃ Bed Temp  ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ GFA08       │ Bambu PLA Sparkle @base        │ PLA       │ Bambu Lab │ 190-230°C   │ 60°C      │
│ GFA01       │ Bambu PLA Matte @base          │ PLA       │ Bambu Lab │ 190-230°C   │ 60°C      │
│ GFB00       │ Bambu ABS @base                │ ABS       │ Bambu Lab │ 240-270°C   │ 80°C      │
└─────────────┴────────────────────────────────┴───────────┴───────────┴─────────────┴───────────┘
```

## Troubleshooting

### Common Issues

1. **Database Connection Error**: Check your `.env` file and database credentials
2. **Table Not Found**: Make sure you've run the SQL script to create the table
3. **GitHub Rate Limiting**: The script includes delays, but if you hit limits, wait and retry
4. **Missing Dependencies**: Install all required packages with pip

### Debug Mode

To see detailed information about what's being fetched, you can modify the script to show raw data or add print statements.
#!/usr/bin/env python3
"""
Script to fetch and parse Bambu Lab filament profiles from GitHub.
Downloads all *@base.json files from the BambuStudio repository and extracts
filament information for database storage.
"""

import os
import sys
import json
import requests
import psycopg2
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, TaskID
import re
from typing import Dict, List, Any, Optional

# Load environment variables
load_dotenv()

# Database configuration
DB_HOST = os.environ.get('DB_HOST')
DB_PORT = os.environ.get('DB_PORT', '5432')
DB_NAME = os.environ.get('DB_NAME')
DB_USER = os.environ.get('DB_USER')
DB_PASSWORD = os.environ.get('DB_PASSWORD')

console = Console()

class BambuFilamentFetcher:
    def __init__(self):
        self.github_api_base = "https://api.github.com/repos/bambulab/BambuStudio/contents/resources/profiles/BBL/filament"
        self.raw_base = "https://raw.githubusercontent.com/bambulab/BambuStudio/master/resources/profiles/BBL/filament"
        self.session = requests.Session()
        self.filaments = []
        
    def fetch_filament_list(self) -> List[Dict[str, Any]]:
        """Fetch list of all files in the filament directory."""
        console.print("[bold blue]Fetching filament file list from GitHub...[/]")
        
        try:
            response = self.session.get(self.github_api_base)
            response.raise_for_status()
            files = response.json()
            
            # Filter for *@base.json files
            base_files = [f for f in files if f['name'].endswith('@base.json')]
            console.print(f"Found [bold]{len(base_files)}[/] @base.json files")
            
            return base_files
            
        except requests.RequestException as e:
            console.print(f"[red]Error fetching file list: {e}[/]")
            return []
    
    def download_and_parse_filament(self, file_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Download and parse a single filament JSON file."""
        filename = file_info['name']
        download_url = f"{self.raw_base}/{filename}"
        
        try:
            response = self.session.get(download_url)
            response.raise_for_status()
            
            # Parse JSON
            filament_data = response.json()
            
            # Extract relevant information
            parsed_filament = self.parse_filament_data(filament_data, filename)
            return parsed_filament
            
        except (requests.RequestException, json.JSONDecodeError) as e:
            console.print(f"[red]Error processing {filename}: {e}[/]")
            return None
    
    def parse_filament_data(self, data: Dict[str, Any], filename: str) -> Dict[str, Any]:
        """Parse filament JSON data into structured format."""
        
        def extract_first_value(field_data):
            """Extract first value from array or return the value directly."""
            if isinstance(field_data, list) and len(field_data) > 0:
                return field_data[0]
            return field_data
        
        def safe_float(value, default=0.0):
            """Safely convert value to float."""
            try:
                if isinstance(value, str):
                    return float(value)
                elif isinstance(value, (int, float)):
                    return float(value)
                return default
            except (ValueError, TypeError):
                return default
        
        def safe_int(value, default=0):
            """Safely convert value to int."""
            try:
                if isinstance(value, str):
                    return int(float(value))  # Handle "190.0" -> 190
                elif isinstance(value, (int, float)):
                    return int(value)
                return default
            except (ValueError, TypeError):
                return default
        
        # Extract basic information
        raw_name = data.get('name', '')
        # Remove @base suffix from name for cleaner display
        clean_name = raw_name.replace(' @base', '').strip()
        
        parsed = {
            'filename': filename,
            'name': clean_name,
            'raw_name': raw_name,  # Keep original for reference
            'filament_id': data.get('filament_id', ''),
            'type': data.get('type', ''),
            'inherits': data.get('inherits', ''),
            'from_source': data.get('from', ''),
            
            # Vendor and cost information
            'vendor': extract_first_value(data.get('filament_vendor', [''])),
            'cost': safe_float(extract_first_value(data.get('filament_cost', [0]))),
            'density': safe_float(extract_first_value(data.get('filament_density', [0]))),
            'flow_ratio': safe_float(extract_first_value(data.get('filament_flow_ratio', [0]))),
            
            # Temperature settings (extract from inherits or direct values)
            'nozzle_temp_min': safe_int(extract_first_value(data.get('nozzle_temperature_range_low', [0]))),
            'nozzle_temp_max': safe_int(extract_first_value(data.get('nozzle_temperature_range_high', [0]))),
            'bed_temp': safe_int(extract_first_value(data.get('bed_temperature', [0]))),
            'bed_temp_initial': safe_int(extract_first_value(data.get('bed_temperature_initial_layer', [0]))),
            
            # Material properties
            'impact_strength_z': safe_float(extract_first_value(data.get('impact_strength_z', [0]))),
            'diameter': safe_float(extract_first_value(data.get('filament_diameter', [1.75]))),
            
            # Advanced settings
            'retraction_length': safe_float(extract_first_value(data.get('retraction_length', [0]))),
            'retraction_speed': safe_float(extract_first_value(data.get('retraction_speed', [0]))),
            'print_speed': safe_float(extract_first_value(data.get('outer_wall_speed', [0]))),
            
            # G-code
            'start_gcode': extract_first_value(data.get('filament_start_gcode', [''])),
            'end_gcode': extract_first_value(data.get('filament_end_gcode', [''])),
            
            # Raw JSON for reference
            'raw_json': json.dumps(data, indent=2)
        }
        
        # Try to extract material type from inherits or name
        if not parsed['nozzle_temp_min'] or not parsed['nozzle_temp_max']:
            # Try to infer from inherits field
            inherits = parsed['inherits'].lower()
            if 'pla' in inherits:
                parsed['material_type'] = 'PLA'
                if not parsed['nozzle_temp_min']: parsed['nozzle_temp_min'] = 190
                if not parsed['nozzle_temp_max']: parsed['nozzle_temp_max'] = 230
                if not parsed['bed_temp']: parsed['bed_temp'] = 60
            elif 'abs' in inherits:
                parsed['material_type'] = 'ABS'
                if not parsed['nozzle_temp_min']: parsed['nozzle_temp_min'] = 240
                if not parsed['nozzle_temp_max']: parsed['nozzle_temp_max'] = 270
                if not parsed['bed_temp']: parsed['bed_temp'] = 80
            elif 'petg' in inherits:
                parsed['material_type'] = 'PETG'
                if not parsed['nozzle_temp_min']: parsed['nozzle_temp_min'] = 230
                if not parsed['nozzle_temp_max']: parsed['nozzle_temp_max'] = 260
                if not parsed['bed_temp']: parsed['bed_temp'] = 70
            elif 'tpu' in inherits:
                parsed['material_type'] = 'TPU'
                if not parsed['nozzle_temp_min']: parsed['nozzle_temp_min'] = 200
                if not parsed['nozzle_temp_max']: parsed['nozzle_temp_max'] = 250
                if not parsed['bed_temp']: parsed['bed_temp'] = 50
            else:
                parsed['material_type'] = 'UNKNOWN'
        else:
            # Try to determine from name or filament_id
            name_lower = parsed['name'].lower()
            if 'pla' in name_lower:
                parsed['material_type'] = 'PLA'
            elif 'abs' in name_lower:
                parsed['material_type'] = 'ABS'
            elif 'petg' in name_lower:
                parsed['material_type'] = 'PETG'
            elif 'tpu' in name_lower:
                parsed['material_type'] = 'TPU'
            else:
                parsed['material_type'] = 'UNKNOWN'
        
        return parsed
    
    def fetch_all_filaments(self) -> List[Dict[str, Any]]:
        """Fetch and parse all filament files."""
        files = self.fetch_filament_list()
        if not files:
            return []
        
        console.print(f"[bold blue]Downloading and parsing {len(files)} filament files...[/]")
        
        with Progress() as progress:
            task = progress.add_task("Processing files...", total=len(files))
            
            for file_info in files:
                filament_data = self.download_and_parse_filament(file_info)
                if filament_data:
                    self.filaments.append(filament_data)
                progress.advance(task)
        
        console.print(f"[green]Successfully parsed {len(self.filaments)} filament profiles[/]")
        return self.filaments
    
    def display_summary(self):
        """Display a summary of fetched filaments."""
        if not self.filaments:
            console.print("[yellow]No filaments to display[/]")
            return
        
        # Create summary table
        table = Table(title="Bambu Lab Filament Profiles Summary")
        table.add_column("Filament ID", style="cyan")
        table.add_column("Name", style="magenta")
        table.add_column("Material", style="green")
        table.add_column("Vendor", style="blue")
        table.add_column("Temp Range", style="red")
        table.add_column("Bed Temp", style="yellow")
        
        for filament in self.filaments[:20]:  # Show first 20
            temp_range = f"{filament['nozzle_temp_min']}-{filament['nozzle_temp_max']}°C" if filament['nozzle_temp_min'] else "N/A"
            bed_temp = f"{filament['bed_temp']}°C" if filament['bed_temp'] else "N/A"
            
            table.add_row(
                filament['filament_id'],
                filament['name'][:30] + "..." if len(filament['name']) > 30 else filament['name'],
                filament['material_type'],
                filament['vendor'],
                temp_range,
                bed_temp
            )
        
        console.print(table)
        if len(self.filaments) > 20:
            console.print(f"[dim]... and {len(self.filaments) - 20} more filaments[/]")
    
    def save_to_database(self):
        """Save filament data to PostgreSQL database."""
        if not self.filaments:
            console.print("[yellow]No filaments to save[/]")
            return
        
        # Validate database configuration
        required_vars = ['DB_HOST', 'DB_NAME', 'DB_USER', 'DB_PASSWORD']
        missing_vars = [var for var in required_vars if not globals()[var]]
        if missing_vars:
            console.print(f"[red]Missing database environment variables: {', '.join(missing_vars)}[/]")
            return
        
        try:
            # Connect to database
            console.print(f"[blue]Connecting to database {DB_NAME} on {DB_HOST}...[/]")
            conn = psycopg2.connect(
                host=DB_HOST, port=DB_PORT, dbname=DB_NAME, 
                user=DB_USER, password=DB_PASSWORD
            )
            cursor = conn.cursor()
            
            # Note: Table should be created manually using create_bambu_filament_table.sql
            console.print("[blue]Assuming bambu_filament_profiles table exists...[/]")
            
            # Insert or update filament data
            insert_sql = """
            INSERT INTO bambu_filament_profiles (
                filename, name, filament_id, type, inherits, from_source, vendor, cost, density, 
                flow_ratio, material_type, nozzle_temp_min, nozzle_temp_max, bed_temp, bed_temp_initial,
                impact_strength_z, diameter, retraction_length, retraction_speed, print_speed,
                start_gcode, end_gcode, raw_json
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON CONFLICT (filament_id) DO UPDATE SET
                filename = EXCLUDED.filename,
                name = EXCLUDED.name,
                type = EXCLUDED.type,
                inherits = EXCLUDED.inherits,
                from_source = EXCLUDED.from_source,
                vendor = EXCLUDED.vendor,
                cost = EXCLUDED.cost,
                density = EXCLUDED.density,
                flow_ratio = EXCLUDED.flow_ratio,
                material_type = EXCLUDED.material_type,
                nozzle_temp_min = EXCLUDED.nozzle_temp_min,
                nozzle_temp_max = EXCLUDED.nozzle_temp_max,
                bed_temp = EXCLUDED.bed_temp,
                bed_temp_initial = EXCLUDED.bed_temp_initial,
                impact_strength_z = EXCLUDED.impact_strength_z,
                diameter = EXCLUDED.diameter,
                retraction_length = EXCLUDED.retraction_length,
                retraction_speed = EXCLUDED.retraction_speed,
                print_speed = EXCLUDED.print_speed,
                start_gcode = EXCLUDED.start_gcode,
                end_gcode = EXCLUDED.end_gcode,
                raw_json = EXCLUDED.raw_json,
                updated_at = CURRENT_TIMESTAMP;
            """
            
            # Insert data with progress bar
            with Progress() as progress:
                task = progress.add_task("Saving to database...", total=len(self.filaments))
                
                for filament in self.filaments:
                    cursor.execute(insert_sql, (
                        filament['filename'], filament['name'], filament['filament_id'],
                        filament['type'], filament['inherits'], filament['from_source'],
                        filament['vendor'], filament['cost'], filament['density'],
                        filament['flow_ratio'], filament['material_type'],
                        filament['nozzle_temp_min'], filament['nozzle_temp_max'],
                        filament['bed_temp'], filament['bed_temp_initial'],
                        filament['impact_strength_z'], filament['diameter'],
                        filament['retraction_length'], filament['retraction_speed'],
                        filament['print_speed'], filament['start_gcode'],
                        filament['end_gcode'], filament['raw_json']
                    ))
                    progress.advance(task)
            
            conn.commit()
            console.print(f"[green]Successfully saved {len(self.filaments)} filament profiles to database[/]")
            
        except psycopg2.Error as e:
            console.print(f"[red]Database error: {e}[/]")
        except Exception as e:
            console.print(f"[red]Error saving to database: {e}[/]")
        finally:
            if 'cursor' in locals():
                cursor.close()
            if 'conn' in locals():
                conn.close()
    
    def save_to_json(self, filename: str = "bambu_filaments.json"):
        """Save filament data to JSON file."""
        if not self.filaments:
            console.print("[yellow]No filaments to save[/]")
            return
        
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(self.filaments, f, indent=2, ensure_ascii=False)
            console.print(f"[green]Saved {len(self.filaments)} filament profiles to {filename}[/]")
        except Exception as e:
            console.print(f"[red]Error saving to JSON: {e}[/]")

def main():
    console.print("[bold cyan]Bambu Lab Filament Profile Fetcher[/]")
    console.print("This script fetches filament profiles from the BambuStudio GitHub repository\n")
    
    fetcher = BambuFilamentFetcher()
    
    # Fetch all filaments
    filaments = fetcher.fetch_all_filaments()
    
    if not filaments:
        console.print("[red]No filaments were fetched. Exiting.[/]")
        sys.exit(1)
    
    # Display summary
    fetcher.display_summary()
    
    # Ask user what to do with the data
    console.print("\n[bold]What would you like to do with the fetched data?[/]")
    console.print("1. Save to database")
    console.print("2. Save to JSON file")
    console.print("3. Both")
    console.print("4. Exit without saving")
    
    choice = input("\nEnter your choice (1-4): ").strip()
    
    if choice in ['1', '3']:
        fetcher.save_to_database()
    
    if choice in ['2', '3']:
        fetcher.save_to_json()
    
    console.print("\n[green]Done![/]")

if __name__ == "__main__":
    main()
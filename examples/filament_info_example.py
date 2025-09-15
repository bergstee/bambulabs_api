#!/usr/bin/env python3
"""
Example script demonstrating how to retrieve filament information from Bambulabs printers.
This shows how to get both the currently active filament and all AMS filament information.
"""

import os
import sys
import bambulabs_api as bl
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

# Load environment variables
load_dotenv()

def display_filament_info(printer_client, printer_name):
    """Display comprehensive filament information for a printer."""
    console = Console()
    
    console.print(f"\n[bold cyan]Filament Information for {printer_name}[/]")
    console.print("=" * 50)
    
    # Get currently active filament
    try:
        vt_tray = printer_client.vt_tray()
        if vt_tray:
            console.print(f"\n[bold green]Currently Active Filament:[/]")
            
            # Create table for active filament
            active_table = Table(show_header=False, box=None, padding=(0, 1))
            active_table.add_column("Property", style="dim", width=15)
            active_table.add_column("Value")
            
            active_table.add_row("Type", f"[magenta]{vt_tray.tray_type}[/]")
            # Remove FF suffix from color if present
            display_color = vt_tray.tray_color
            if (display_color and display_color not in ['N/A', ''] and
                len(display_color) == 8 and display_color.endswith('FF')):
                display_color = display_color[:-2]
            active_table.add_row("Color", f"[yellow]{display_color}[/]" if display_color != 'N/A' else "[dim]N/A[/]")
            active_table.add_row("Brand", vt_tray.tray_sub_brands if vt_tray.tray_sub_brands != 'N/A' else "[dim]N/A[/]")
            active_table.add_row("Weight", f"{vt_tray.tray_weight}g" if vt_tray.tray_weight != 'N/A' else "[dim]N/A[/]")
            active_table.add_row("Temp Range", f"{vt_tray.nozzle_temp_min}-{vt_tray.nozzle_temp_max}°C")
            active_table.add_row("Bed Temp", f"{vt_tray.bed_temp}°C" if vt_tray.bed_temp != 'N/A' else "[dim]N/A[/]")
            active_table.add_row("UUID", vt_tray.tray_uuid if vt_tray.tray_uuid != 'N/A' else "[dim]N/A[/]")
            
            console.print(active_table)
        else:
            console.print("[yellow]No active filament detected[/]")
    except Exception as e:
        console.print(f"[red]Error retrieving active filament: {e}[/]")
    
    # Get AMS information
    try:
        ams_hub = printer_client.ams_hub()
        console.print(f"\n[bold blue]AMS Hub Information:[/]")
        
        found_ams = False
        for ams_id in range(4):  # Check up to 4 AMS units
            try:
                ams = ams_hub[ams_id]
                found_ams = True
                
                console.print(f"\n[blue]AMS Unit {ams_id}:[/]")
                console.print(f"  Humidity: {ams.humidity}%")
                console.print(f"  Temperature: {ams.temperature}°C")
                
                # Check for filament trays
                tray_found = False
                for tray_id in range(4):  # Check up to 4 trays per AMS
                    tray = ams.get_filament_tray(tray_id)
                    if tray:
                        if not tray_found:
                            console.print(f"  [bold]Filament Trays:[/]")
                            tray_found = True
                        
                        # Create table for this tray
                        tray_table = Table(show_header=False, box=None, padding=(0, 1))
                        tray_table.add_column("Property", style="dim", width=12)
                        tray_table.add_column("Value")
                        
                        tray_table.add_row("Tray", f"[cyan]{tray_id}[/]")
                        tray_table.add_row("Type", f"[magenta]{tray.tray_type}[/]")
                        # Remove FF suffix from color if present
                        tray_display_color = tray.tray_color
                        if (tray_display_color and tray_display_color not in ['N/A', ''] and
                            len(tray_display_color) == 8 and tray_display_color.endswith('FF')):
                            tray_display_color = tray_display_color[:-2]
                        tray_table.add_row("Color", f"[yellow]{tray_display_color}[/]" if tray_display_color != 'N/A' else "[dim]N/A[/]")
                        tray_table.add_row("Brand", tray.tray_sub_brands if tray.tray_sub_brands != 'N/A' else "[dim]N/A[/]")
                        tray_table.add_row("Weight", f"{tray.tray_weight}g" if tray.tray_weight != 'N/A' else "[dim]N/A[/]")
                        tray_table.add_row("Temp Range", f"{tray.nozzle_temp_min}-{tray.nozzle_temp_max}°C")
                        tray_table.add_row("Bed Temp", f"{tray.bed_temp}°C" if tray.bed_temp != 'N/A' else "[dim]N/A[/]")
                        
                        console.print(f"    Tray {tray_id}:")
                        console.print(tray_table)
                        console.print()
                
                if not tray_found:
                    console.print("  [dim]No filament trays detected[/]")
                    
            except KeyError:
                # AMS unit doesn't exist
                continue
        
        if not found_ams:
            console.print("[yellow]No AMS units detected[/]")
            
    except Exception as e:
        console.print(f"[red]Error retrieving AMS information: {e}[/]")

def main():
    console = Console()
    
    # Example configuration - replace with your printer details
    PRINTER_IP = "192.168.1.100"  # Replace with your printer's IP
    ACCESS_CODE = "12345678"      # Replace with your access code
    SERIAL = "01S00A123456789"    # Replace with your printer's serial
    
    console.print("[bold cyan]Bambulabs Filament Information Example[/]")
    console.print("This example shows how to retrieve filament information from your printer.")
    console.print("\n[yellow]Note: Update the PRINTER_IP, ACCESS_CODE, and SERIAL variables with your printer's details.[/]")
    
    try:
        # Initialize printer client
        console.print(f"\nConnecting to printer at {PRINTER_IP}...")
        printer = bl.Printer(PRINTER_IP, ACCESS_CODE, SERIAL)
        
        # Start MQTT connection
        console.print("Starting MQTT connection...")
        printer.mqtt_start()
        
        # Wait for MQTT to be ready
        import time
        console.print("Waiting for MQTT client to receive initial data...")
        start_time = time.time()
        timeout = 10
        
        while time.time() - start_time < timeout:
            if printer.mqtt_client.ready():
                console.print("[green]MQTT client is ready![/]")
                break
            time.sleep(0.5)
        else:
            console.print("[red]Timeout: MQTT client did not become ready[/]")
            return
        
        # Display filament information
        display_filament_info(printer, "Example Printer")
        
    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
    finally:
        # Clean up
        try:
            if 'printer' in locals():
                printer.mqtt_stop()
                console.print("\n[green]MQTT connection closed.[/]")
        except:
            pass

if __name__ == "__main__":
    main()
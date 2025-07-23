#!/usr/bin/env python3
"""
Simple runner script for the Bambu Lab filament fetcher.
This script provides an easy way to run the fetcher with different options.
"""

import sys
import os

# Add the parent directory to the path so we can import the fetcher
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.fetch_bambu_filaments import BambuFilamentFetcher
from rich.console import Console

def main():
    console = Console()
    
    console.print("[bold cyan]Bambu Lab Filament Profile Fetcher[/]")
    console.print("This script fetches filament profiles from the BambuStudio GitHub repository\n")
    
    console.print("[yellow]Before running this script, make sure you have:[/]")
    console.print("1. Created the database table using: scripts/create_bambu_filament_table.sql")
    console.print("2. Set up your .env file with database credentials")
    console.print("3. Installed required dependencies: pip install requests psycopg2 rich python-dotenv\n")
    
    # Ask user if they want to continue
    response = input("Do you want to continue? (y/N): ").strip().lower()
    if response not in ['y', 'yes']:
        console.print("[yellow]Exiting...[/]")
        return
    
    fetcher = BambuFilamentFetcher()
    
    # Fetch all filaments
    console.print("\n[bold blue]Step 1: Fetching filament data from GitHub...[/]")
    filaments = fetcher.fetch_all_filaments()
    
    if not filaments:
        console.print("[red]No filaments were fetched. Exiting.[/]")
        sys.exit(1)
    
    # Display summary
    console.print("\n[bold blue]Step 2: Displaying summary...[/]")
    fetcher.display_summary()
    
    # Ask user what to do with the data
    console.print("\n[bold]What would you like to do with the fetched data?[/]")
    console.print("1. Save to database")
    console.print("2. Save to JSON file")
    console.print("3. Both")
    console.print("4. Exit without saving")
    
    while True:
        choice = input("\nEnter your choice (1-4): ").strip()
        if choice in ['1', '2', '3', '4']:
            break
        console.print("[red]Invalid choice. Please enter 1, 2, 3, or 4.[/]")
    
    if choice in ['1', '3']:
        console.print("\n[bold blue]Step 3: Saving to database...[/]")
        fetcher.save_to_database()
    
    if choice in ['2', '3']:
        console.print("\n[bold blue]Step 3: Saving to JSON file...[/]")
        fetcher.save_to_json()
    
    if choice == '4':
        console.print("[yellow]Exiting without saving.[/]")
    
    console.print("\n[green]Done![/]")

if __name__ == "__main__":
    main()
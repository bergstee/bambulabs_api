import os
import sys
import time
import bambulabs_api as bl
import psycopg2
from dotenv import load_dotenv
import subprocess
import platform
import datetime
import logging # Import the logging module
from rich.console import Console
from rich.table import Table

# Load environment variables from .env file
load_dotenv()

# --- Database Configuration ---
DB_HOST = os.environ.get('DB_HOST')
DB_PORT = os.environ.get('DB_PORT', '5432')
DB_NAME = os.environ.get('DB_NAME')
DB_USER = os.environ.get('DB_USER')
DB_PASSWORD = os.environ.get('DB_PASSWORD')

# --- Helper Functions ---
def ping_host(host):
    """ Pings a host to check reachability. """
    param = '-n' if platform.system().lower() == 'windows' else '-c'
    command = ['ping', param, '1', host]
    try:
        response = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        return response.returncode == 0
    except subprocess.TimeoutExpired:
        # print(f"Ping to {host} timed out.") # Optional: Less verbose during loop
        return False
    except Exception as e:
        # Log the exception, but don't necessarily print to console unless needed
        logging.exception(f"Error during ping to host {host}")
        # print(f"Error during ping to {host}: {e}") # Keep console clean
        return False

# --- Main Script Logic ---
if __name__ == '__main__':
    # --- Logging Configuration ---
    log_file = os.path.join(os.path.dirname(__file__), 'monitor_printers.log')
    logging.basicConfig(
        level=logging.ERROR, # Log ERROR level and above
        format='%(asctime)s - %(levelname)s - %(message)s',
        filename=log_file,
        filemode='a' # Append to the log file
    )
    # --- End Logging Configuration ---

    console = Console(legacy_windows=True) # Initialize Rich Console for Windows compatibility
    console.print("[bold cyan]Starting Bambulabs Printer Continuous Monitoring Script...[/]")
    logging.info("Monitoring script started.") # Log script start (INFO level won't go to file by default, but good practice)

    # Validate environment variables
    required_vars = ['DB_HOST', 'DB_NAME', 'DB_USER', 'DB_PASSWORD']
    missing_vars = [var for var in required_vars if not globals()[var]]
    if missing_vars:
        console.print(f"[bold red]Error:[/bold red] Missing required environment variables: {', '.join(missing_vars)}")
        sys.exit(1)

    db_conn = None
    # List to store dicts: {"id": ..., "name": ..., "client": ...,
    #                       "previous_status": None, "previous_filename": None, "last_log_timestamp": 0}
    active_printers = []

    try:
        # --- Database Connection ---
        console.print(f"Connecting to database '[bold yellow]{DB_NAME}[/]' on [bold yellow]{DB_HOST}:{DB_PORT}[/]...")
        db_conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD
        )
        console.print("[green]Database connection successful.[/]")

        # --- Fetch Printer Data ---
        cur = db_conn.cursor()
        console.print("Fetching printer details from the database...")
        cur.execute("SELECT printer_id, printer_name, printer_ip, printer_bambu_id, access_code FROM printers;")
        printers_data = cur.fetchall()
        cur.close() # Close cursor immediately after fetching
        console.print(f"Found [bold]{len(printers_data)}[/] printers.")

        if not printers_data:
            console.print("[yellow]No printers found in the database table 'printers'. Exiting.[/]")
            sys.exit(0)

        # --- Initialize and Connect Printers ---
        for printer_id, name, ip, serial, access_code in printers_data: # Added printer_id
            console.print(f"\n--- Initializing Printer: [bold magenta]{name}[/] (ID: {printer_id}, IP: {ip}) ---")

            if not all([printer_id, ip, serial, access_code]): # Check printer_id too
                console.print(f"[yellow]Warning:[/yellow] Skipping printer '[bold magenta]{name}[/]' due to missing IP, Serial, or Access Code.")
                continue

            # Ping Check
            console.print(f"Pinging [bold magenta]{name}[/] at {ip}...")
            if not ping_host(ip):
                console.print(f"[red]Printer {name} ({ip}) is not reachable via ping. Skipping.[/]")
                continue
            console.print(f"[green]Printer {name} ({ip}) is reachable.[/]")

            # Create and Connect
            try:
                console.print(f"Initializing API for [bold magenta]{name}[/] (Serial: {serial})...")
                printer_client = bl.Printer(ip, access_code, serial)

                console.print(f"Starting MQTT for [bold magenta]{name}[/]...")
                printer_client.mqtt_start()
                console.print(f"[green]MQTT started for {name}.[/]")

                # Add to active list *before* waiting for ready,
                # so cleanup still happens if ready check fails/times out
                # Initialize previous state tracking and current job ID
                active_printers.append({
                    "id": printer_id,
                    "name": name,
                    "client": printer_client,
                    "previous_status": None,
                    "previous_filename": None,
                    "last_log_timestamp": 0, # Initialize last log time
                    "current_job_id": None # Initialize current job ID
                })

                # Wait for MQTT client to be ready
                console.print("Waiting for MQTT client to receive initial data...")
                start_time = time.time()
                timeout = 10 # seconds
                ready = False
                while time.time() - start_time < timeout:
                    if printer_client.mqtt_client.ready():
                        console.print(f"[green]MQTT client for {name} is ready.[/]")
                        ready = True
                        break
                    time.sleep(0.5)

                if not ready:
                    console.print(f"[yellow]Timeout:[/yellow] MQTT client for {name} did not become ready within {timeout} seconds.")
                    # Note: We keep it in active_printers for potential later connection / cleanup

            except Exception as e:
                error_msg = f"Error initializing or starting MQTT for {name}"
                console.print(f"[bold red]{error_msg}:[/bold red] {e}")
                logging.exception(error_msg) # Log exception with traceback
                # Ensure partial connections are cleaned up if init fails mid-way
                if 'printer_client' in locals() and printer_client and hasattr(printer_client, 'mqtt_client') and printer_client.mqtt_client.is_connected():
                     try:
                         printer_client.mqtt_stop()
                     except Exception: pass # Ignore errors during cleanup


        if not active_printers:
            console.print("[bold red]No printers could be successfully initialized and connected via MQTT. Exiting.[/]")
            sys.exit(0)

# --- Initialization for Unreachable Printer Retry Logic ---
        unreachable_printers = [] # List to hold (printer_id, name, ip, serial, access_code) tuples
        RETRY_INTERVAL_SECONDS = 300 # Check every 5 minutes
        last_retry_attempt_time = 0 # Initialize to ensure first check runs if needed
        console.print("\n[bold cyan]--- Starting Continuous Monitoring Loop (Press Ctrl+C to stop) ---[/]")

        # --- Continuous Monitoring Loop ---
        while True:
            console.print("-" * 40, style="dim") # Separator for each monitoring cycle
            # --- Function to Update Printers Table ---
            # Added p_percentage parameter
            def update_printer_table(p_id, p_status, p_remaining_time_min, p_gcode_file, p_percentage):
                update_cur = None
                try:
                    now = datetime.datetime.now()
                    update_cur = db_conn.cursor()

                    # Convert remaining time (minutes) to seconds for DB (integer column)
                    remaining_seconds = None
                    if isinstance(p_remaining_time_min, (int, float)) and p_remaining_time_min >= 0:
                        remaining_seconds = int(p_remaining_time_min * 60)

                    # Convert percentage to float or None
                    progress_float = None
                    if isinstance(p_percentage, (int, float)) and 0 <= p_percentage <= 100:
                        progress_float = float(p_percentage)

                    # Update printers table
                    update_sql = """
                        UPDATE printers
                        SET last_poll_status = %s,
                            last_polled_at = %s,
                            remaining_time = %s,
                            current_print_job = %s,
                            Print_Progress = %s -- Added progress update
                        WHERE printer_id = %s;
                    """
                    update_cur.execute(update_sql, (p_status, now, remaining_seconds, p_gcode_file, progress_float, p_id))
                    db_conn.commit()
                    # console.print(f"  Printers table updated for printer ID {p_id}") # Optional

                except Exception as db_update_e:
                    error_msg = f"Error updating printers table for printer ID {p_id}"
                    console.print(f"  [red]{error_msg}:[/red] {db_update_e}")
                    logging.exception(error_msg) # Log exception with traceback
                    if db_conn: db_conn.rollback()
                finally:
                    if update_cur: update_cur.close()
            # --- End Function to Update Printers Table ---

            # --- Function to Update Filament Information ---
            def update_filament_table(p_id, p_current_filament_info, p_ams_filament_info):
                filament_cur = None
                try:
                    now = datetime.datetime.now()
                    filament_cur = db_conn.cursor()
                    
                    # Get the printer's AMS count to determine which filament records to store
                    filament_cur.execute("""
                        SELECT ams_count FROM printers WHERE printer_id = %s
                    """, (p_id,))
                    result = filament_cur.fetchone()
                    ams_count = result[0] if result else 0
                    
                    # Check if we have any filament data to process
                    # For P1 series, don't delete existing data if no filament info is received
                    has_filament_data = False
                    if ams_count == 0:
                        # For non-AMS printers, check if we have current filament info
                        has_filament_data = p_current_filament_info is not None
                    else:
                        # For AMS printers, check if we have AMS filament info
                        has_filament_data = p_ams_filament_info is not None and len(p_ams_filament_info) > 0
                    
                    # Only proceed if we have filament data to process
                    if not has_filament_data:
                        # console.print(f"  No filament data received for printer ID {p_id}, skipping update") # Optional
                        return
                    
                    # Delete existing records and recreate to ensure clean state
                    # This prevents duplicate records and ensures we only have current data
                    filament_cur.execute("""
                        DELETE FROM printer_filaments WHERE printer_id = %s
                    """, (p_id,))
                    
                    # Store filament records based on AMS count
                    if ams_count == 0:
                        # Printer has no AMS - store only external spool (tray_id = NULL)
                        if p_current_filament_info:
                            db_info = p_current_filament_info.get('db_info')
                            
                            # Prepare filament data
                            filament_data = {
                                'filament_id': p_current_filament_info.get('tray_info_idx'),
                                'filament_name': db_info.get('db_name') if db_info else None,
                                'filament_type': p_current_filament_info.get('type'),
                                'filament_color': p_current_filament_info.get('color'),
                                'filament_vendor': db_info.get('db_vendor') if db_info else p_current_filament_info.get('brand'),
                                'temp_min': db_info.get('db_temp_min') if db_info else p_current_filament_info.get('temp_min'),
                                'temp_max': db_info.get('db_temp_max') if db_info else p_current_filament_info.get('temp_max'),
                                'bed_temp': db_info.get('db_bed_temp') if db_info else p_current_filament_info.get('bed_temp'),
                                'weight': p_current_filament_info.get('weight'),
                                'cost': db_info.get('db_cost') if db_info else None,
                                'density': db_info.get('db_density') if db_info else None,
                                'diameter': db_info.get('db_diameter') if db_info else p_current_filament_info.get('diameter'),
                                'tray_uuid': p_current_filament_info.get('tray_uuid')
                            }
                            
                            # Insert external spool filament (ams_id and tray_id are NULL)
                            filament_cur.execute("""
                                INSERT INTO printer_filaments (
                                    printer_id, ams_id, tray_id, filament_id, filament_name, filament_type,
                                    filament_color, filament_vendor, temp_min, temp_max, bed_temp,
                                    weight, cost, density, diameter, tray_uuid, is_active, detected_at, updated_at
                                ) VALUES (
                                    %s, NULL, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s
                                )
                            """, (
                                p_id, filament_data['filament_id'], filament_data['filament_name'],
                                filament_data['filament_type'], filament_data['filament_color'],
                                filament_data['filament_vendor'], filament_data['temp_min'],
                                filament_data['temp_max'], filament_data['bed_temp'],
                                filament_data['weight'], filament_data['cost'],
                                filament_data['density'], filament_data['diameter'],
                                filament_data['tray_uuid'], now, now
                            ))
                    
                    else:
                        # Printer has AMS units - store exactly 4 records per AMS unit
                        for ams_id in range(ams_count):
                            # Create a map of existing tray data for this AMS
                            existing_trays = {}
                            if p_ams_filament_info:
                                for ams in p_ams_filament_info:
                                    if ams['ams_id'] == ams_id:
                                        for tray in ams['trays']:
                                            existing_trays[tray['tray_id']] = tray
                                        break
                            
                            # Insert records for all 4 tray positions (0-3)
                            for tray_id in range(4):
                                if tray_id in existing_trays:
                                    # Use actual tray data
                                    tray = existing_trays[tray_id]
                                    db_info = tray.get('db_info')
                                    
                                    # Prepare tray data
                                    tray_data = {
                                        'filament_id': tray.get('tray_info_idx'),
                                        'filament_name': db_info.get('db_name') if db_info else None,
                                        'filament_type': tray.get('type'),
                                        'filament_color': tray.get('color')[:-2] if (tray.get('color') and len(tray.get('color')) == 8 and tray.get('color').endswith('FF')) else tray.get('color'),
                                        'filament_vendor': db_info.get('db_vendor') if db_info else tray.get('brand'),
                                        'temp_min': db_info.get('db_temp_min') if db_info else tray.get('temp_min'),
                                        'temp_max': db_info.get('db_temp_max') if db_info else tray.get('temp_max'),
                                        'bed_temp': db_info.get('db_bed_temp') if db_info else tray.get('bed_temp'),
                                        'weight': tray.get('weight'),
                                        'cost': db_info.get('db_cost') if db_info else None,
                                        'density': db_info.get('db_density') if db_info else None,
                                        'diameter': db_info.get('db_diameter') if db_info else tray.get('diameter'),
                                        'tray_uuid': tray.get('uuid')
                                    }
                                    
                                    # Determine if this is the currently active filament
                                    is_active = (p_current_filament_info and
                                                p_current_filament_info.get('tray_info_idx') == tray_data['filament_id'])
                                else:
                                    # Create NULL record for missing/empty tray
                                    tray_data = {
                                        'filament_id': None,
                                        'filament_name': None,
                                        'filament_type': None,
                                        'filament_color': None,
                                        'filament_vendor': None,
                                        'temp_min': None,
                                        'temp_max': None,
                                        'bed_temp': None,
                                        'weight': None,
                                        'cost': None,
                                        'density': None,
                                        'diameter': None,
                                        'tray_uuid': None
                                    }
                                    is_active = False
                                
                                # Insert AMS tray filament (actual data or NULL placeholder)
                                filament_cur.execute("""
                                    INSERT INTO printer_filaments (
                                        printer_id, ams_id, tray_id, filament_id, filament_name, filament_type,
                                        filament_color, filament_vendor, temp_min, temp_max, bed_temp,
                                        weight, cost, density, diameter, tray_uuid, is_active, detected_at, updated_at
                                    ) VALUES (
                                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                                    )
                                """, (
                                    p_id, ams_id, tray_id, tray_data['filament_id'],
                                    tray_data['filament_name'], tray_data['filament_type'],
                                    tray_data['filament_color'], tray_data['filament_vendor'],
                                    tray_data['temp_min'], tray_data['temp_max'], tray_data['bed_temp'],
                                    tray_data['weight'], tray_data['cost'], tray_data['density'],
                                    tray_data['diameter'], tray_data['tray_uuid'], is_active, now, now
                                ))
                    
                    db_conn.commit()
                    # console.print(f"  Filament information updated for printer ID {p_id} (AMS count: {ams_count})") # Optional
                    
                except Exception as filament_update_e:
                    error_msg = f"Error updating filament information for printer ID {p_id}"
                    console.print(f"  [red]{error_msg}:[/red] {filament_update_e}")
                    logging.exception(error_msg)
                    if db_conn: db_conn.rollback()
                finally:
                    if filament_cur: filament_cur.close()
            # --- End Function to Update Filament Information ---

            # --- Function to Log Status Periodically ---
            LOG_INTERVAL_SECONDS = 300 # 5 minutes
            def log_status_periodically(printer_info_dict, p_status):
                log_cur = None
                p_id = printer_info_dict["id"]
                last_log_time = printer_info_dict.get("last_log_timestamp", 0)
                current_time = time.time()

                if current_time - last_log_time >= LOG_INTERVAL_SECONDS:
                    try:
                        now_dt = datetime.datetime.now()
                        log_cur = db_conn.cursor()
                        log_sql = """
                            INSERT INTO printer_status_logs (printer_id, status, logged_at)
                            VALUES (%s, %s, %s);
                        """
                        log_cur.execute(log_sql, (p_id, p_status, now_dt))
                        db_conn.commit()
                        printer_info_dict["last_log_timestamp"] = current_time # Update last log time
                        # console.print(f"  Status logged for printer ID {p_id}") # Optional
                    except Exception as db_log_e:
                        error_msg = f"Error inserting status log for printer ID {p_id}"
                        console.print(f"  [red]{error_msg}:[/red] {db_log_e}")
                        logging.exception(error_msg) # Log exception with traceback
                        if db_conn: db_conn.rollback()
                    finally:
                        if log_cur: log_cur.close()
            # --- End Function to Log Status Periodically ---

            # --- Function to Log Job Filament Information ---
            def log_job_filament(job_history_id, p_id, current_filament_info):
                """Store filament information used for this print job"""
                if not current_filament_info or not job_history_id:
                    return

                filament_cur = None
                try:
                    filament_cur = db_conn.cursor()

                    db_info = current_filament_info.get('db_info', {}) or {}

                    insert_sql = """
                        INSERT INTO printer_job_filaments (
                            job_history_id, printer_id, filament_id, tray_uuid,
                            filament_name, filament_type, filament_color,
                            filament_vendor, temp_min, temp_max, bed_temp,
                            weight, cost, density, diameter
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (job_history_id) DO NOTHING;
                    """

                    filament_cur.execute(insert_sql, (
                        job_history_id, p_id,
                        current_filament_info.get('tray_info_idx'),
                        current_filament_info.get('tray_uuid'),
                        db_info.get('db_name') if db_info else None,
                        current_filament_info.get('type'),
                        current_filament_info.get('color'),
                        db_info.get('db_vendor') if db_info else current_filament_info.get('brand'),
                        current_filament_info.get('temp_min'),
                        current_filament_info.get('temp_max'),
                        current_filament_info.get('bed_temp'),
                        current_filament_info.get('weight'),
                        db_info.get('db_cost') if db_info else None,
                        db_info.get('db_density') if db_info else None,
                        db_info.get('db_diameter') if db_info else current_filament_info.get('diameter')
                    ))

                    db_conn.commit()

                    # Build display string for confirmation
                    filament_display = current_filament_info.get('type', 'Unknown')
                    if current_filament_info.get('color'):
                        filament_display += f" - {current_filament_info.get('color')}"
                    if db_info and db_info.get('db_name'):
                        filament_display += f" ({db_info.get('db_name')})"

                    console.print(f"  [green]Filament data captured:[/green] {filament_display}")

                except Exception as e:
                    error_msg = f"Error logging filament for job {job_history_id}"
                    console.print(f"  [red]{error_msg}:[/red] {e}")
                    logging.exception(error_msg)
                    if db_conn: db_conn.rollback()
                finally:
                    if filament_cur: filament_cur.close()
            # --- End Function to Log Job Filament Information ---

            # --- Job History Logging Function ---
            # Added remaining_time_min and percentage parameters
            def log_job_event(p_id, current_status, current_filename, prev_status, prev_filename, remaining_time_min, percentage, current_filament_info):
                job_cur = None
                try:
                    now = datetime.datetime.now() # Time of detection
                    job_cur = db_conn.cursor()
                    is_running = current_status == "RUNNING"
                    was_running = prev_status == "RUNNING"

                    # --- Job Start Detection ---
                    # Check: Was not running before, is running now, and has a valid filename
                    if not was_running and is_running and current_filename:
                        # console.print(f"  Detected potential Job Start: {current_filename}") # Less verbose

                        # Check if an unfinished job with the same name already exists for this printer
                        check_sql = """
                            SELECT COUNT(*)
                            FROM printer_job_history
                            WHERE printer_id = %s
                              AND filename = %s
                              AND end_time IS NULL;
                        """
                        job_cur.execute(check_sql, (p_id, current_filename))
                        existing_unfinished_count = job_cur.fetchone()[0]

                        if existing_unfinished_count == 0:
                            # console.print(f"  No existing unfinished job found. Logging new job start.") # Less verbose

                            # --- Calculate Total Print Time Interval ---
                            interval_string = None
                            remaining_seconds = None # Need remaining seconds for start time calc
                            if isinstance(remaining_time_min, (int, float)) and remaining_time_min >= 0:
                                remaining_seconds = int(remaining_time_min * 60)
                                # Estimate total time based on remaining and percentage
                                if isinstance(percentage, (int, float)) and 0 < percentage < 100:
                                    try:
                                        # total = remaining * 100 / (100 - percentage)
                                        # Use float division for accuracy
                                        estimated_total_seconds = int(float(remaining_seconds) * 100.0 / (100.0 - float(percentage)))
                                        interval_string = f"{estimated_total_seconds} seconds"
                                    except ZeroDivisionError:
                                        interval_string = f"{remaining_seconds} seconds" # Fallback
                                    except Exception:
                                        interval_string = f"{remaining_seconds} seconds" # Fallback
                                else:
                                     # If percentage is 0 or 100 or invalid, use remaining as total estimate
                                     interval_string = f"{remaining_seconds} seconds"
                                # console.print(f"  Calculated estimated total print time: {interval_string}") # Less verbose
                            # else:
                                # console.print(f"  Could not calculate estimated total print time (remaining_time_min: {remaining_time_min})") # Less verbose
                            # --- End Calculate Total Print Time Interval ---


                            # --- Estimate Actual Start Time ---
                            actual_start_time = now # Default to detection time
                            if isinstance(percentage, (int, float)) and 0 < percentage < 100 and remaining_seconds is not None:
                                try:
                                    # elapsed_seconds = (percentage * remaining_seconds) / (100 - percentage)
                                    # Use float division for accuracy
                                    elapsed_seconds = (float(percentage) * float(remaining_seconds)) / (100.0 - float(percentage))
                                    actual_start_time = now - datetime.timedelta(seconds=elapsed_seconds)
                                    # console.print(f"  Estimated actual start time: {actual_start_time.strftime('%Y-%m-%d %H:%M:%S')} (Detected: {now.strftime('%Y-%m-%d %H:%M:%S')})") # Less verbose
                                except ZeroDivisionError:
                                    # This case should be avoided by the 0 < percentage < 100 check, but handle defensively
                                    console.print(f"  [yellow]Warning:[/yellow] Cannot estimate start time due to percentage calculation issue (percentage={percentage}). Using detection time.")
                                except Exception as est_e:
                                    console.print(f"  [yellow]Warning:[/yellow] Error estimating start time: {est_e}. Using detection time.")
                            # else:
                                # console.print(f"  Could not estimate actual start time (Percentage: {percentage}, Remaining Secs: {remaining_seconds}). Using detection time.") # Less verbose
                            # --- End Estimate Actual Start Time ---


                            start_sql = """
                                INSERT INTO printer_job_history (printer_id, filename, start_time, status, total_print_time)
                                VALUES (%s, %s, %s, %s, %s::interval) RETURNING id; -- Cast the string parameter to interval and return ID
                            """
                            # Pass the interval_string and actual_start_time to the parameters
                            job_cur.execute(start_sql, (p_id, current_filename, actual_start_time, current_status, interval_string))
                            # Fetch the returned job ID
                            job_id = job_cur.fetchone()[0]
                            db_conn.commit()
                            console.print(f"  [green]Job history START logged:[/green] Printer ID {p_id}, File: [cyan]{current_filename}[/], Est. Start: {actual_start_time.strftime('%H:%M:%S')}, Job ID: {job_id}")

                            # Store the current job ID in the printer_info_dict
                            # Find the correct printer_info_dict in the active_printers list
                            for printer in active_printers:
                                if printer["id"] == p_id:
                                    printer["current_job_id"] = job_id
                                    break

                            # Capture filament information for this job
                            log_job_filament(job_id, p_id, current_filament_info)

                        # else:
                            # console.print(f"  Skipping job start log: Found {existing_unfinished_count} existing unfinished job(s) for printer ID {p_id}, file: {current_filename}") # Less verbose
                            # No commit needed if we didn't insert

                    # --- Job End Detection ---
                    # Check: Was running before, is not running now, and had a valid previous filename
                    elif was_running and not is_running and prev_filename:
                        # console.print(f"  Detected Job End: {prev_filename} -> {current_status}") # Less verbose
                        end_sql = """
                            UPDATE printer_job_history
                            SET end_time = %s, status = %s
                            WHERE printer_id = %s AND filename = %s AND end_time IS NULL;
                        """
                        # Update the last unfinished job for this printer and filename
                        job_cur.execute(end_sql, (now, current_status, p_id, prev_filename))
                        # Check if any row was updated
                        if job_cur.rowcount > 0:
                             db_conn.commit()
                             console.print(f"  [blue]Job history END logged:[/blue] Printer ID {p_id}, File: [cyan]{prev_filename}[/], Status: {current_status}")

                             # --- Stock Transaction will be handled by database trigger ---
                             if current_status == "FINISH":
                                 console.print(f"  [green]Job finished successfully.[/green] Stock transaction will be handled by database trigger for file: [cyan]{prev_filename}[/]")
                                 # Reset the job ID in the active_printers list
                                 for printer in active_printers:
                                     if printer["id"] == p_id:
                                         printer["current_job_id"] = None
                                         break
                             # --- End Stock Transaction Logic ---

                        else:
                             console.print(f"  [yellow]Warning:[/yellow] Could not find matching unfinished job history record for printer ID {p_id}, file: {prev_filename} to mark as ended.")
                             db_conn.rollback() # Rollback if no record found to update


                except Exception as job_log_e:
                    error_msg = f"Error logging job event for printer ID {p_id}"
                    console.print(f"  [red]{error_msg}:[/red] {job_log_e}")
                    logging.exception(error_msg) # Log exception with traceback
                    if db_conn:
                        db_conn.rollback()
                finally:
                    if job_cur:
                        job_cur.close()
            # --- End Job History Logging Function ---


            # --- Retry Unreachable Printers ---
            current_time = time.time()
            if unreachable_printers and (current_time - last_retry_attempt_time >= RETRY_INTERVAL_SECONDS):
                console.print(f"\n[bold blue]--- Attempting to reconnect unreachable printers ({len(unreachable_printers)} found) ---[/]")
                last_retry_attempt_time = current_time # Update last attempt time
                printers_to_remove_from_unreachable = [] # Keep track of successfully reconnected printers

                for printer_data_tuple in unreachable_printers:
                    printer_id, name, ip, serial, access_code = printer_data_tuple
                    console.print(f"  Retrying connection for: [bold magenta]{name}[/] (ID: {printer_id}, IP: {ip})")

                    # 1. Ping Check
                    if not ping_host(ip):
                        console.print(f"    [yellow]Ping failed for {name} ({ip}). Will retry later.[/]")
                        continue # Try next unreachable printer
                    console.print(f"    [green]Ping successful for {name} ({ip}).[/]")

                    # 2. Create and Connect
                    try:
                        console.print(f"    Initializing API for [bold magenta]{name}[/]...")
                        printer_client = bl.Printer(ip, access_code, serial)
                        console.print(f"    Starting MQTT for [bold magenta]{name}[/]...")
                        printer_client.mqtt_start()
                        console.print(f"    [green]MQTT started for {name}.[/]")

                        # 3. Wait for MQTT ready
                        console.print("    Waiting for MQTT client to receive initial data...")
                        start_wait_time = time.time()
                        timeout = 10 # seconds
                        ready = False
                        while time.time() - start_wait_time < timeout:
                            if printer_client.mqtt_client.ready():
                                console.print(f"    [green]MQTT client for {name} is ready.[/]")
                                ready = True
                                break
                            time.sleep(0.5)

                        if ready:
                            console.print(f"    [bold green]Successfully reconnected to {name}! Adding to active list.[/]")
                            # Add to active list
                            active_printers.append({
                                "id": printer_id,
                                "name": name,
                                "client": printer_client,
                                "previous_status": None, # Initialize state
                                "previous_filename": None,
                                "last_log_timestamp": 0,
                                "current_job_id": None
                            })
                            # Mark for removal from unreachable list
                            printers_to_remove_from_unreachable.append(printer_data_tuple)
                        else:
                            console.print(f"    [yellow]Timeout:[/yellow] MQTT client for {name} did not become ready. Will retry later.")
                            # Ensure MQTT is stopped if connection failed mid-way
                            if printer_client and hasattr(printer_client, 'mqtt_client') and printer_client.mqtt_client.is_connected():
                                try: printer_client.mqtt_stop()
                                except Exception: pass

                    except Exception as retry_e:
                        error_msg = f"Error during retry connection for {name}"
                        console.print(f"    [bold red]{error_msg}:[/bold red] {retry_e}")
                        logging.exception(f"{error_msg} (during retry)") # Log exception with traceback
                        # Ensure MQTT is stopped if connection failed mid-way
                        if 'printer_client' in locals() and printer_client and hasattr(printer_client, 'mqtt_client') and printer_client.mqtt_client.is_connected():
                            try: printer_client.mqtt_stop()
                            except Exception: pass

                # Remove successfully reconnected printers from the unreachable list
                if printers_to_remove_from_unreachable:
                    unreachable_printers = [p for p in unreachable_printers if p not in printers_to_remove_from_unreachable]
                    console.print(f"[bold blue]--- Finished retry attempt. {len(unreachable_printers)} printers remaining unreachable. ---[/]")
            # --- End Retry Unreachable Printers ---

            for printer_info in active_printers:
                printer_id = printer_info["id"]
                name = printer_info["name"]
                client = printer_info["client"]
                previous_status = printer_info.get("previous_status")
                previous_filename = printer_info.get("previous_filename")
                # No need to get last_log_timestamp here, function handles it

                console.print(f"\nChecking Active Printer: [bold magenta]{name}[/] (ID: {printer_id})") # Added newline for clarity

                # Check if MQTT client is still connected and ready
                if not client.mqtt_client.is_connected():
                    console.print(f"  [yellow]MQTT disconnected for {name}. Attempting reconnect...[/]")
                    try:
                        # Attempt to restart MQTT (might need more robust reconnect logic)
                        client.mqtt_stop() # Ensure clean state
                        time.sleep(1)
                        client.mqtt_start()
                        console.print(f"  MQTT restart attempted for {name}.")
                        # Give it a moment after restart attempt
                        time.sleep(2)
                        if not client.mqtt_client.is_connected():
                             console.print(f"  [red]Reconnect failed for {name}.[/]")
                             continue # Skip this printer for this cycle
                    except Exception as recon_e:
                        error_msg = f"Error during reconnect attempt for {name}"
                        console.print(f"  [red]{error_msg}:[/red] {recon_e}")
                        logging.exception(error_msg) # Log exception with traceback
                        continue # Skip this printer for this cycle


                if not client.mqtt_client.ready():
                    console.print(f"  [yellow]MQTT client for {name} not ready (waiting for data).[/]")
                    continue # Skip status check if not ready

                # Get detailed status
                try:
                    status = client.get_state()
                    percentage = client.get_percentage() # Expecting a number 0-100
                    gcode_file = client.gcode_file() or "[dim]N/A[/]" # Handle empty filename
                    layer_num = client.current_layer_num()
                    total_layer_num = client.total_layer_num()
                    bed_temp = client.get_bed_temperature()
                    nozzle_temp = client.get_nozzle_temperature()
                    remaining_time_min = client.get_time() # In minutes
                    
                    # Get filament information with database lookup
                    current_filament_info = None
                    ams_filament_info = []
                    
                    def get_filament_from_database(filament_id):
                        """Look up filament information from the database."""
                        if not filament_id or filament_id == 'N/A':
                            return None
                        
                        try:
                            db_cur = db_conn.cursor()
                            db_cur.execute("""
                                SELECT name, material_type, vendor, nozzle_temp_min, nozzle_temp_max,
                                       bed_temp, density, cost, diameter, flow_ratio
                                FROM bambu_filament_profiles
                                WHERE filament_id = %s
                            """, (filament_id,))
                            result = db_cur.fetchone()
                            db_cur.close()
                            
                            if result:
                                return {
                                    'db_name': result[0],
                                    'db_material_type': result[1],
                                    'db_vendor': result[2],
                                    'db_temp_min': result[3],
                                    'db_temp_max': result[4],
                                    'db_bed_temp': result[5],
                                    'db_density': result[6],
                                    'db_cost': result[7],
                                    'db_diameter': result[8],
                                    'db_flow_ratio': result[9]
                                }
                        except Exception as e:
                            console.print(f"  [dim]Database lookup error for {filament_id}: {e}[/]")
                        return None
                    
                    try:
                        # Get currently active filament - check if vt_tray data exists first
                        vt_tray_data = client.mqtt_client._PrinterMQTTClient__get_print("vt_tray")
                        if vt_tray_data is not None and isinstance(vt_tray_data, dict):
                            vt_tray = client.vt_tray()
                            if vt_tray:
                                # Extract filament information with proper validation and type conversion
                                tray_type = getattr(vt_tray, 'tray_type', 'N/A')
                                tray_color_raw = getattr(vt_tray, 'tray_color', 'N/A')
                                # Remove FF suffix if present (8-char hex -> 6-char hex)
                                if (tray_color_raw and tray_color_raw not in ['N/A', ''] and
                                    len(tray_color_raw) == 8 and tray_color_raw.endswith('FF')):
                                    tray_color = tray_color_raw[:-2]
                                else:
                                    tray_color = tray_color_raw
                                tray_weight = getattr(vt_tray, 'tray_weight', 'N/A')
                                tray_brand = getattr(vt_tray, 'tray_sub_brands', 'N/A')
                                tray_info_idx = getattr(vt_tray, 'tray_info_idx', 'N/A')
                                tray_diameter = getattr(vt_tray, 'tray_diameter', 'N/A')
                                bed_temp = getattr(vt_tray, 'bed_temp', 'N/A')
                                
                                # Convert temperature values to integers, handling string inputs
                                temp_min = 0
                                temp_max = 0
                                bed_temp_int = 0
                                try:
                                    temp_min_raw = getattr(vt_tray, 'nozzle_temp_min', 0)
                                    temp_max_raw = getattr(vt_tray, 'nozzle_temp_max', 0)
                                    temp_min = int(temp_min_raw) if temp_min_raw not in ['N/A', '', None] else 0
                                    temp_max = int(temp_max_raw) if temp_max_raw not in ['N/A', '', None] else 0
                                    bed_temp_int = int(bed_temp) if bed_temp not in ['N/A', '', None] else 0
                                except (ValueError, TypeError):
                                    temp_min = temp_max = bed_temp_int = 0
                                
                                # Look up filament information from database
                                db_filament_info = get_filament_from_database(tray_info_idx)
                                
                                # Try to get specific filament type from the Filament enum (fallback)
                                specific_filament_name = None
                                try:
                                    if tray_info_idx and tray_info_idx != 'N/A':
                                        # Try to find matching filament by tray_info_idx
                                        from bambulabs_api.filament_info import Filament
                                        for filament_enum in Filament:
                                            if filament_enum.tray_info_idx == tray_info_idx:
                                                specific_filament_name = filament_enum.name
                                                break
                                except Exception:
                                    pass  # If we can't match, just use the basic type
                                
                                # Only create filament info if we have meaningful data
                                if (tray_type and tray_type not in ['N/A', ''] and
                                    (temp_min > 0 or temp_max > 0 or db_filament_info or
                                     (tray_color and tray_color not in ['N/A', '', '000000', '00000000']))):
                                    current_filament_info = {
                                        'type': tray_type,
                                        'specific_type': specific_filament_name,
                                        'color': tray_color,
                                        'temp_min': temp_min,
                                        'temp_max': temp_max,
                                        'bed_temp': bed_temp_int,
                                        'weight': tray_weight,
                                        'brand': tray_brand,
                                        'diameter': tray_diameter,
                                        'tray_info_idx': tray_info_idx,
                                        'db_info': db_filament_info  # Add database info
                                    }
                    except Exception as filament_e:
                        # Only show warning if it's not just missing vt_tray data
                        if "NoneType" not in str(filament_e):
                            console.print(f"  [yellow]Warning:[/yellow] Could not retrieve current filament info: {filament_e}")
                    
                    try:
                        # Get AMS hub information
                        ams_hub = client.ams_hub()
                        for ams_id in range(4):  # Check up to 4 AMS units
                            try:
                                ams = ams_hub[ams_id]
                                ams_info = {
                                    'ams_id': ams_id,
                                    'humidity': ams.humidity,
                                    'temperature': ams.temperature,
                                    'trays': []
                                }
                                
                                # Get filament trays in this AMS
                                for tray_id in range(4):  # Check up to 4 trays per AMS
                                    tray = ams.get_filament_tray(tray_id)
                                    if tray:
                                        tray_info_idx = getattr(tray, 'tray_info_idx', 'N/A')
                                        
                                        # Look up filament information from database
                                        db_filament_info = get_filament_from_database(tray_info_idx)
                                        
                                        # Try to get specific filament type from enum (fallback)
                                        specific_filament_name = None
                                        try:
                                            if tray_info_idx and tray_info_idx != 'N/A':
                                                from bambulabs_api.filament_info import Filament
                                                for filament_enum in Filament:
                                                    if filament_enum.tray_info_idx == tray_info_idx:
                                                        specific_filament_name = filament_enum.name
                                                        break
                                        except Exception:
                                            pass
                                        
                                        tray_info = {
                                            'tray_id': tray_id,
                                            'type': tray.tray_type,
                                            'specific_type': specific_filament_name,
                                            'color': tray.tray_color[:-2] if (tray.tray_color and len(tray.tray_color) == 8 and tray.tray_color.endswith('FF')) else tray.tray_color,
                                            'temp_min': tray.nozzle_temp_min,
                                            'temp_max': tray.nozzle_temp_max,
                                            'weight': tray.tray_weight,
                                            'brand': tray.tray_sub_brands,
                                            'uuid': tray.tray_uuid,
                                            'diameter': getattr(tray, 'tray_diameter', 'N/A'),
                                            'bed_temp': getattr(tray, 'bed_temp', 'N/A'),
                                            'tray_info_idx': tray_info_idx,
                                            'db_info': db_filament_info  # Add database info
                                        }
                                        ams_info['trays'].append(tray_info)
                                
                                if ams_info['trays']:  # Only add AMS if it has trays
                                    ams_filament_info.append(ams_info)
                                    
                            except KeyError:
                                # AMS unit doesn't exist, continue to next
                                continue
                    except Exception as ams_e:
                        console.print(f"  [yellow]Warning:[/yellow] Could not retrieve AMS info: {ams_e}")

                    # --- Format Status with Color ---
                    status_color = "white"
                    if status == "RUNNING": status_color = "green"
                    elif status == "FINISH": status_color = "blue"
                    elif status == "FAILED": status_color = "red"
                    elif status == "IDLE": status_color = "yellow"
                    status_str = f"[{status_color}]{status}[/]"

                    # --- Format Progress Bar ---
                    progress_bar = "[dim]N/A[/]"
                    if isinstance(percentage, (int, float)) and 0 <= percentage <= 100:
                        bar_length = 20 # Length of the progress bar
                        filled_length = int(bar_length * percentage / 100)
                        bar = '#' * filled_length + '-' * (bar_length - filled_length)
                        progress_bar = f"[cyan]|{bar}| {percentage:.1f}%[/]"
                    elif percentage == "Unknown":
                        progress_bar = "[dim]Unknown[/]"


                    # --- Format Finish Time ---
                    finish_time_str = "[dim]N/A[/]"
                    if isinstance(remaining_time_min, (int, float)) and remaining_time_min >= 0:
                        try:
                            finish_time = datetime.datetime.now() + datetime.timedelta(minutes=int(remaining_time_min))
                            finish_time_str = finish_time.strftime("%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            finish_time_str = "[red]Invalid Time[/]"
                    elif remaining_time_min == "Unknown":
                         finish_time_str = "[dim]Unknown[/]"

                    # --- Format Remaining Time ---
                    remaining_str = f"{remaining_time_min} min" if isinstance(remaining_time_min, (int, float)) else "[dim]Unknown[/]"

                    # --- Create Rich Table ---
                    table = Table(show_header=False, box=None, padding=(0, 1))
                    table.add_column("Attribute", style="dim", width=12)
                    table.add_column("Value")

                    table.add_row("Status", status_str)
                    table.add_row("Progress", progress_bar)
                    table.add_row("File", f"[cyan]{gcode_file}[/]")
                    table.add_row("Layer", f"{layer_num}/{total_layer_num}")
                    table.add_row("Bed Temp", f"{bed_temp} C")
                    table.add_row("Nozzle Temp", f"{nozzle_temp} C")
                    table.add_row("Remaining", remaining_str)
                    table.add_row("Est. Finish", finish_time_str)
                    
                    # --- Add Current Filament Information ---
                    if current_filament_info:
                        db_info = current_filament_info.get('db_info')
                        
                        # Build filament display - prioritize database info
                        if db_info and db_info.get('db_name'):
                            # Use database name (more descriptive)
                            filament_display = f"[magenta]{db_info['db_name']}[/]"
                        elif current_filament_info.get('specific_type'):
                            # Fallback to enum name
                            filament_display = f"[magenta]{current_filament_info['specific_type']}[/]"
                        else:
                            # Final fallback to basic type
                            filament_display = f"[magenta]{current_filament_info['type']}[/]"
                        
                        # Add color if available
                        if current_filament_info['color'] and current_filament_info['color'] not in ['N/A', '', '000000', '00000000']:
                            filament_display += f" - [yellow]{current_filament_info['color']}[/]"
                        
                        # Add vendor from database if available
                        if db_info and db_info.get('db_vendor'):
                            filament_display += f" ([blue]{db_info['db_vendor']}[/])"
                        elif (current_filament_info['brand'] and
                              current_filament_info['brand'] not in ['N/A', ''] and
                              not current_filament_info.get('specific_type')):
                            filament_display += f" ({current_filament_info['brand']})"
                        
                        table.add_row("Active Filament", filament_display)
                        
                        # Add nozzle temperature range - prioritize database values
                        temp_min = current_filament_info.get('temp_min', 0)
                        temp_max = current_filament_info.get('temp_max', 0)
                        
                        if db_info and db_info.get('db_temp_min') and db_info.get('db_temp_max'):
                            # Use database temperatures (more accurate)
                            db_temp_min = db_info['db_temp_min']
                            db_temp_max = db_info['db_temp_max']
                            if db_temp_min != db_temp_max:
                                temp_range = f"{db_temp_min}-{db_temp_max}°C [green](DB)[/]"
                            else:
                                temp_range = f"{db_temp_min}°C [green](DB)[/]"
                            table.add_row("Nozzle Temp", temp_range)
                        elif temp_min > 0 and temp_max > 0:
                            if temp_min != temp_max:
                                temp_range = f"{temp_min}-{temp_max}°C"
                            else:
                                temp_range = f"{temp_min}°C"
                            table.add_row("Nozzle Temp", temp_range)
                        elif temp_min > 0:
                            table.add_row("Nozzle Temp", f"{temp_min}°C")
                        elif temp_max > 0:
                            table.add_row("Nozzle Temp", f"{temp_max}°C")
                        
                        # Add bed temperature - prioritize database values
                        bed_temp = current_filament_info.get('bed_temp', 0)
                        if db_info and db_info.get('db_bed_temp'):
                            table.add_row("Bed Temp Rec", f"{db_info['db_bed_temp']}°C [green](DB)[/]")
                        elif bed_temp > 0:
                            table.add_row("Bed Temp Rec", f"{bed_temp}°C")
                        
                        # Add diameter - prioritize database values
                        if db_info and db_info.get('db_diameter'):
                            table.add_row("Diameter", f"{db_info['db_diameter']:.2f}mm [green](DB)[/]")
                        else:
                            diameter = current_filament_info.get('diameter', 'N/A')
                            if diameter and diameter not in ['N/A', '']:
                                table.add_row("Diameter", f"{diameter}mm")
                        
                        # Add additional database information
                        if db_info:
                            if db_info.get('db_cost'):
                                table.add_row("Cost/kg", f"${db_info['db_cost']:.2f} [green](DB)[/]")
                            if db_info.get('db_density'):
                                table.add_row("Density", f"{db_info['db_density']:.2f} g/cm³ [green](DB)[/]")
                        
                        # Add weight if available
                        weight = current_filament_info.get('weight', 'N/A')
                        if weight and weight not in ['N/A', '', '0']:
                            table.add_row("Weight", f"{weight}g")
                    else:
                        table.add_row("Active Filament", "[dim]N/A[/]")

                    console.print(table)
                    
                    # --- Display AMS Information ---
                    if ams_filament_info:
                        console.print(f"  [bold blue]AMS Information:[/]")
                        for ams in ams_filament_info:
                            console.print(f"    [blue]AMS {ams['ams_id']}:[/] {ams['humidity']}% humidity, {ams['temperature']}°C")
                            for tray in ams['trays']:
                                db_info = tray.get('db_info')
                                
                                # Build tray display - prioritize database info
                                if db_info and db_info.get('db_name'):
                                    # Use database name (more descriptive)
                                    tray_display = f"[magenta]{db_info['db_name']}[/]"
                                elif tray.get('specific_type'):
                                    tray_display = f"[magenta]{tray['specific_type']}[/]"
                                else:
                                    tray_display = f"[magenta]{tray['type']}[/]"
                                
                                # Add color
                                if tray['color'] and tray['color'] not in ['N/A', '', '000000', '00000000']:
                                    tray_display += f" - [yellow]{tray['color']}[/]"
                                
                                # Add vendor from database if available
                                if db_info and db_info.get('db_vendor'):
                                    tray_display += f" ([blue]{db_info['db_vendor']}[/])"
                                elif (tray['brand'] and tray['brand'] not in ['N/A', ''] and
                                      not tray.get('specific_type')):
                                    tray_display += f" ({tray['brand']})"
                                
                                # Add weight
                                if tray['weight'] and tray['weight'] not in ['N/A', '', '0']:
                                    tray_display += f" [{tray['weight']}g]"
                                
                                # Add temperature info - prioritize database values
                                temp_info = ""
                                if db_info and db_info.get('db_temp_min') and db_info.get('db_temp_max'):
                                    # Use database temperatures (more accurate)
                                    db_temp_min = db_info['db_temp_min']
                                    db_temp_max = db_info['db_temp_max']
                                    if db_temp_min != db_temp_max:
                                        temp_info = f" ([green]{db_temp_min}-{db_temp_max}°C DB[/])"
                                    else:
                                        temp_info = f" ([green]{db_temp_min}°C DB[/])"
                                elif (isinstance(tray.get('temp_min'), int) and isinstance(tray.get('temp_max'), int) and
                                      tray['temp_min'] > 0 and tray['temp_max'] > 0):
                                    if tray['temp_min'] != tray['temp_max']:
                                        temp_info = f" ({tray['temp_min']}-{tray['temp_max']}°C)"
                                    else:
                                        temp_info = f" ({tray['temp_min']}°C)"
                                
                                # Add cost info if available from database
                                cost_info = ""
                                if db_info and db_info.get('db_cost'):
                                    cost_info = f" [dim]${db_info['db_cost']:.2f}/kg[/]"
                                
                                console.print(f"      Tray {tray['tray_id']}: {tray_display}{temp_info}{cost_info}")


                    # --- Update Printers Table (Every Cycle) ---
                    # Pass percentage to the update function
                    update_printer_table(printer_id, status, remaining_time_min, gcode_file if gcode_file != "[dim]N/A[/]" else None, percentage)
                    # --- End Update Printers Table ---

                    # --- Update Filament Information (Every Cycle) ---
                    update_filament_table(printer_id, current_filament_info, ams_filament_info)
                    # --- End Update Filament Information ---

                    # --- Log Status Periodically (Every 5 Mins) ---
                    log_status_periodically(printer_info, status)
                    # --- End Log Status Periodically ---

                    # --- Log Job History Event (On State Change) ---
                    # Pass necessary info including remaining_time_min, percentage, and filament info
                    log_job_event(printer_id, status, gcode_file, previous_status, previous_filename, remaining_time_min, percentage, current_filament_info)
                    # --- End Log Job History Event ---

                    # --- Update Previous State for next iteration ---
                    printer_info["previous_status"] = status
                    printer_info["previous_filename"] = gcode_file
                    # --- End Update Previous State ---

                except Exception as status_e:
                    error_msg = f"Error retrieving status for {name}"
                    console.print(f"  [red]{error_msg}:[/red] {status_e}")
                    logging.exception(error_msg) # Log exception with traceback
                    # Optionally update DB with error status?
                    # update_printer_table(printer_id, "ERROR", None, None)
                    # Don't update job history or previous state on error

            # Wait before next full cycle (including potential retry)
            console.print("\n" + "-" * 40, style="dim")
            console.print(f"Waiting for 10 seconds before next check cycle...", style="dim")
            time.sleep(10) # Check every 10 seconds

    except KeyboardInterrupt:
        console.print("\n[yellow]Ctrl+C detected. Stopping monitoring...[/]")
        logging.info("KeyboardInterrupt detected. Stopping monitoring.")
    except psycopg2.Error as db_e:
        error_msg = "Database Error occurred"
        console.print(f"[bold red]{error_msg}:[/bold red] {db_e}")
        logging.exception(error_msg) # Log exception with traceback
    except Exception as e:
        error_msg = "An unexpected error occurred in the main loop"
        console.print(f"[bold red]{error_msg}:[/bold red] {e}")
        logging.exception(error_msg) # Log exception with traceback
    finally:
        # --- Cleanup ---
        console.print("\n[cyan]Cleaning up connections...[/]")
        for printer_info in active_printers:
            name = printer_info["name"]
            client = printer_info["client"]
            if client and hasattr(client, 'mqtt_client') and client.mqtt_client.is_connected(): # Added hasattr check
                try:
                    console.print(f"Stopping MQTT for [bold magenta]{name}[/]...")
                    client.mqtt_stop()
                    console.print(f"[green]MQTT stopped for {name}.[/]")
                except Exception as disconnect_e:
                    error_msg = f"Error stopping MQTT for {name}"
                    console.print(f"[red]{error_msg}:[/red] {disconnect_e}")
                    logging.exception(error_msg) # Log exception with traceback

        if db_conn:
            db_conn.close()
            console.print("[green]Database connection closed.[/]")

    console.print("\n[bold cyan]Printer monitoring script finished.[/]")
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
        response = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
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

    console = Console() # Initialize Rich Console
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

            # --- Job History Logging Function ---
            # Added remaining_time_min and percentage parameters
            def log_job_event(p_id, current_status, current_filename, prev_status, prev_filename, remaining_time_min, percentage):
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
                        bar = '█' * filled_length + '-' * (bar_length - filled_length)
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
                    table.add_row("Bed Temp", f"{bed_temp}°C")
                    table.add_row("Nozzle Temp", f"{nozzle_temp}°C")
                    table.add_row("Remaining", remaining_str)
                    table.add_row("Est. Finish", finish_time_str)

                    console.print(table)


                    # --- Update Printers Table (Every Cycle) ---
                    # Pass percentage to the update function
                    update_printer_table(printer_id, status, remaining_time_min, gcode_file if gcode_file != "[dim]N/A[/]" else None, percentage)
                    # --- End Update Printers Table ---

                    # --- Log Status Periodically (Every 5 Mins) ---
                    log_status_periodically(printer_info, status)
                    # --- End Log Status Periodically ---

                    # --- Log Job History Event (On State Change) ---
                    # Pass necessary info including remaining_time_min and percentage
                    log_job_event(printer_id, status, gcode_file, previous_status, previous_filename, remaining_time_min, percentage)
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
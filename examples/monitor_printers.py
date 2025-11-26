import os
import sys
import time
import bambulabs_api as bl
import psycopg2
from psycopg2 import pool
from dotenv import load_dotenv
import subprocess
import platform
import datetime
import logging
import logging.handlers  # Add this explicit import
import json
from rich.console import Console
from rich.table import Table
from typing import Optional, Dict, List, Tuple
import threading
from contextlib import contextmanager

# Load environment variables from .env file
load_dotenv()

# --- Database Configuration ---
DB_HOST = os.environ.get('DB_HOST')
DB_PORT = os.environ.get('DB_PORT', '5432')
DB_NAME = os.environ.get('DB_NAME')
DB_USER = os.environ.get('DB_USER')
DB_PASSWORD = os.environ.get('DB_PASSWORD')

# --- Connection Pool Configuration ---
MIN_CONNECTIONS = 1
MAX_CONNECTIONS = 5
CONNECTION_TIMEOUT = 30  # seconds
RECONNECT_DELAY = 5  # seconds
MAX_RECONNECT_ATTEMPTS = 3

# --- Printer Connection Configuration ---
PRINTER_TIMEOUT = 30  # seconds for printer operations
PRINTER_HEALTH_CHECK_INTERVAL = 60  # seconds
MAX_CONSECUTIVE_FAILURES = 3  # before moving to unreachable

class DatabaseConnectionManager:
    """Manages database connections with automatic reconnection."""
    
    def __init__(self):
        self.pool = None
        self.console = Console(legacy_windows=True)
        self._initialize_pool()
    
    def _initialize_pool(self):
        """Initialize the connection pool with retry logic."""
        for attempt in range(MAX_RECONNECT_ATTEMPTS):
            try:
                self.pool = psycopg2.pool.ThreadedConnectionPool(
                    MIN_CONNECTIONS,
                    MAX_CONNECTIONS,
                    host=DB_HOST,
                    port=DB_PORT,
                    database=DB_NAME,
                    user=DB_USER,
                    password=DB_PASSWORD,
                    connect_timeout=CONNECTION_TIMEOUT
                )
                self.console.print("[green]Database connection pool initialized.[/]")
                return
            except Exception as e:
                self.console.print(f"[yellow]Database connection attempt {attempt + 1} failed: {e}[/]")
                if attempt < MAX_RECONNECT_ATTEMPTS - 1:
                    time.sleep(RECONNECT_DELAY)
                else:
                    raise
    
    @contextmanager
    def get_connection(self):
        """Context manager for database connections with automatic cleanup."""
        conn = None
        try:
            conn = self.pool.getconn()
            yield conn
            conn.commit()
        except psycopg2.OperationalError as e:
            logging.error(f"Database operational error: {e}")
            if conn:
                conn.rollback()
            # Try to reinitialize the pool
            self._initialize_pool()
            raise
        except Exception as e:
            logging.error(f"Database error: {e}")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                self.pool.putconn(conn)
    
    def execute_query(self, query, params=None, fetch=False):
        """Execute a query with automatic retry on connection failure."""
        for attempt in range(MAX_RECONNECT_ATTEMPTS):
            try:
                with self.get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(query, params)
                        if fetch:
                            return cur.fetchall()
                        return cur.rowcount
            except psycopg2.OperationalError as e:
                logging.error(f"Query attempt {attempt + 1} failed: {e}")
                if attempt < MAX_RECONNECT_ATTEMPTS - 1:
                    time.sleep(RECONNECT_DELAY)
                else:
                    raise
    
    def close(self):
        """Close all connections in the pool."""
        if self.pool:
            self.pool.closeall()

class PrinterConnectionManager:
    """Manages printer connections with health checking and automatic recovery."""
    
    def __init__(self, printer_id, name, ip, serial, access_code, db_manager, mqtt_logger=None):
        self.printer_id = printer_id
        self.name = name
        self.ip = ip
        self.serial = serial
        self.access_code = access_code
        self.db_manager = db_manager
        self.mqtt_logger = mqtt_logger
        self.client = None
        self.console = Console(legacy_windows=True)
        self.consecutive_failures = 0
        self.last_successful_poll = time.time()
        self.is_connected = False
        self.lock = threading.Lock()

        # State tracking
        self.previous_status = None
        self.previous_filename = None
        self.last_log_timestamp = 0
        self.current_job_id = None
        self.needs_filament_backfill = False  # Flag to backfill filament info for loaded jobs
    
    def connect(self) -> bool:
        """Establish connection to the printer with proper error handling."""
        with self.lock:
            try:
                # Clean up any existing connection
                self._cleanup_connection()
                
                # Ping check first
                if not self._ping_host():
                    self.console.print(f"[yellow]Printer {self.name} not reachable via ping.[/]")
                    return False
                
                # Create and connect
                self.console.print(f"Connecting to {self.name}...")
                self.client = bl.Printer(self.ip, self.access_code, self.serial)
                self.client.mqtt_start()
                
                # Wait for ready with timeout
                start_time = time.time()
                while time.time() - start_time < PRINTER_TIMEOUT:
                    if self.client.mqtt_client.ready():
                        self.is_connected = True
                        self.consecutive_failures = 0
                        self.last_successful_poll = time.time()
                        self.console.print(f"[green]Connected to {self.name}[/]")

                        # Load any ongoing job from database
                        self._load_ongoing_job()

                        return True
                    time.sleep(0.5)
                
                # Timeout occurred
                self._cleanup_connection()
                return False
                
            except Exception as e:
                logging.error(f"Failed to connect to {self.name}: {e}")
                self._cleanup_connection()
                return False
    
    def _ping_host(self) -> bool:
        """Ping the printer to check basic network connectivity."""
        param = '-n' if platform.system().lower() == 'windows' else '-c'
        command = ['ping', param, '1', self.ip]
        try:
            response = subprocess.run(
                command, 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL, 
                timeout=3
            )
            return response.returncode == 0
        except (subprocess.TimeoutExpired, Exception):
            return False
    
    def _cleanup_connection(self):
        """Clean up existing MQTT connection."""
        if self.client:
            try:
                if hasattr(self.client, 'mqtt_client'):
                    if self.client.mqtt_client.is_connected():
                        self.client.mqtt_stop()
            except Exception as e:
                logging.error(f"Error during cleanup for {self.name}: {e}")
            finally:
                self.client = None
                self.is_connected = False

    def _load_ongoing_job(self):
        """Load any ongoing job from database on startup to track it properly."""
        try:
            # Query for unfinished jobs for this printer
            result = self.db_manager.execute_query(
                """
                SELECT id, filename, start_time
                FROM printer_job_history
                WHERE printer_id = %s
                  AND end_time IS NULL
                ORDER BY start_time DESC
                LIMIT 1;
                """,
                (self.printer_id,),
                fetch=True
            )

            if result and len(result) > 0:
                job_id, filename, start_time = result[0]
                self.current_job_id = job_id
                self.previous_filename = filename
                self.previous_status = "RUNNING"  # Assume it's running since end_time is NULL

                self.console.print(f"  [cyan]Loaded ongoing job:[/] ID={job_id}, File={filename}, Started={start_time}")
                logging.info(f"Printer {self.name}: Loaded ongoing job ID {job_id} ({filename}) from database")

                # Check if filament info exists for this job
                filament_count = self.db_manager.execute_query(
                    """
                    SELECT COUNT(*) FROM printer_job_filaments
                    WHERE job_history_id = %s;
                    """,
                    (job_id,),
                    fetch=True
                )

                if filament_count and filament_count[0][0] == 0:
                    self.needs_filament_backfill = True
                    self.console.print(f"  [yellow]No filament info found for job {job_id} - will backfill on next poll[/]")
                    logging.info(f"Printer {self.name}: Job {job_id} needs filament backfill")
                else:
                    self.console.print(f"  [dim]Filament info already exists for job {job_id}[/]")
            else:
                self.console.print(f"  [dim]No ongoing job found in database for {self.name}[/]")

        except Exception as e:
            logging.error(f"Failed to load ongoing job for {self.name}: {e}")
            self.console.print(f"  [red]Error loading ongoing job: {e}[/]")

    def check_health(self) -> bool:
        """Check if the printer connection is healthy."""
        with self.lock:
            if not self.client:
                return False
            
            try:
                # Check MQTT connection
                if not self.client.mqtt_client.is_connected():
                    return False
                
                # Check if client is ready
                if not self.client.mqtt_client.ready():
                    return False
                
                # Try to get basic status
                _ = self.client.get_state()
                
                self.last_successful_poll = time.time()
                self.consecutive_failures = 0
                return True
                
            except Exception as e:
                logging.error(f"Health check failed for {self.name}: {e}")
                self.consecutive_failures += 1
                return False
    
    def get_status_safe(self) -> Optional[Dict]:
        """Get printer status with timeout and error handling."""
        if not self.is_connected:
            return None
        
        with self.lock:
            try:
                # Use threading to implement timeout for status retrieval
                result = {}
                error = None
                
                def fetch_status():
                    nonlocal result, error
                    try:
                        # Get raw print data for tray_now field
                        raw_data = self.client.mqtt_client.dump()

                        # Log raw MQTT data to file
                        if self.mqtt_logger:
                            mqtt_json = json.dumps(raw_data, indent=2)
                            self.mqtt_logger.info(f"Printer: {self.name}\n{mqtt_json}")

                        print_data = raw_data.get('print', {})
                        ams_data = print_data.get('ams', {})

                        result = {
                            'status': self.client.get_state(),
                            'percentage': self.client.get_percentage(),
                            'gcode_file': self.client.gcode_file(),
                            'layer_num': self.client.current_layer_num(),
                            'total_layer_num': self.client.total_layer_num(),
                            'bed_temp': self.client.get_bed_temperature(),
                            'nozzle_temp': self.client.get_nozzle_temperature(),
                            'remaining_time_min': self.client.get_time(),
                            'vt_tray': self._get_vt_tray_safe(),
                            'ams_hub': self._get_ams_hub_safe(),
                            'tray_now': ams_data.get('tray_now'),  # Active tray ID (from ams object)
                            'tray_tar': ams_data.get('tray_tar')   # Target tray ID (from ams object)
                        }
                    except Exception as e:
                        error = e
                
                thread = threading.Thread(target=fetch_status)
                thread.daemon = True
                thread.start()
                thread.join(timeout=PRINTER_TIMEOUT)
                
                if thread.is_alive():
                    logging.error(f"Status fetch timeout for {self.name}")
                    self.consecutive_failures += 1
                    return None
                
                if error:
                    raise error
                
                self.consecutive_failures = 0
                self.last_successful_poll = time.time()
                return result
                
            except Exception as e:
                logging.error(f"Failed to get status for {self.name}: {e}")
                self.consecutive_failures += 1
                
                # Mark for reconnection if too many failures
                if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    self.is_connected = False
                    self._cleanup_connection()
                
                return None
    
    def _get_vt_tray_safe(self):
        """Safely get vt_tray information."""
        try:
            vt_tray_data = self.client.mqtt_client._PrinterMQTTClient__get_print("vt_tray")
            if vt_tray_data is not None and isinstance(vt_tray_data, dict):
                return self.client.vt_tray()
        except Exception:
            return None
        return None
    
    def _get_ams_hub_safe(self):
        """Safely get AMS hub information."""
        try:
            return self.client.ams_hub()
        except Exception:
            return None
    
    def reconnect(self) -> bool:
        """Attempt to reconnect to the printer."""
        self.console.print(f"[yellow]Attempting to reconnect to {self.name}...[/]")
        return self.connect()
    
    def disconnect(self):
        """Disconnect from the printer."""
        with self.lock:
            self._cleanup_connection()
            self.console.print(f"[yellow]Disconnected from {self.name}[/]")

class SafePrinterMonitor:
    """Main monitoring class with enhanced safety features."""
    
    def __init__(self):
        self.console = Console(legacy_windows=True)
        self.db_manager = DatabaseConnectionManager()
        self.printer_managers = []
        self.unreachable_printers = []
        self.running = True
        self.RETRY_INTERVAL_SECONDS = 300
        self.last_retry_attempt_time = 0
        
        # Set up logging
        self._setup_logging()
    
    def _setup_logging(self):
        """Configure logging with rotation."""
        log_file = os.path.join(os.path.dirname(__file__), 'monitor_printers.log')

        # Create handlers
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5
        )
        console_handler = logging.StreamHandler()

        # Set format
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        # Configure root logger
        logger = logging.getLogger()
        logger.setLevel(logging.ERROR)
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

        # Set up MQTT message logger (keeps last ~200 lines)
        mqtt_log_file = os.path.join(os.path.dirname(__file__), 'mqtt_messages.log')
        mqtt_logger = logging.getLogger('mqtt_messages')
        mqtt_logger.setLevel(logging.INFO)
        mqtt_logger.propagate = False  # Don't propagate to root logger

        # Each line is roughly 500 bytes, so 100KB should hold ~200 lines
        mqtt_file_handler = logging.handlers.RotatingFileHandler(
            mqtt_log_file,
            maxBytes=100*1024,  # 100KB for ~200 lines
            backupCount=1  # Keep only 1 backup file
        )
        mqtt_formatter = logging.Formatter('%(asctime)s - %(message)s')
        mqtt_file_handler.setFormatter(mqtt_formatter)
        mqtt_logger.addHandler(mqtt_file_handler)

        # Store reference for later use
        self.mqtt_logger = mqtt_logger
    
    def initialize_printers(self):
        """Initialize all printers from database where in_production is true."""
        try:
            printers_data = self.db_manager.execute_query(
                "SELECT printer_id, printer_name, printer_ip, printer_bambu_id, access_code FROM printers WHERE in_production = true;",
                fetch=True
            )
            
            if not printers_data:
                self.console.print("[yellow]No printers found in database.[/]")
                return
            
            for printer_id, name, ip, serial, access_code in printers_data:
                if not all([printer_id, ip, serial, access_code]):
                    self.console.print(f"[yellow]Skipping {name} due to missing data.[/]")
                    continue
                
                manager = PrinterConnectionManager(
                    printer_id, name, ip, serial, access_code, self.db_manager, self.mqtt_logger
                )
                
                if manager.connect():
                    self.printer_managers.append(manager)
                else:
                    self.unreachable_printers.append(
                        (printer_id, name, ip, serial, access_code)
                    )
            
        except Exception as e:
            logging.error(f"Failed to initialize printers: {e}")
            raise
    
    def monitor_loop(self):
        """Main monitoring loop with enhanced error handling."""
        self.console.print("[bold cyan]Starting continuous monitoring...[/]")
        self.console.print(f"Monitoring {len(self.printer_managers)} active printers")
        self.console.print(f"{len(self.unreachable_printers)} printers currently unreachable")
        self.console.print("[yellow]Press Ctrl+C to stop[/]\n")
        
        cycle_count = 0
        while self.running:
            try:
                cycle_count += 1
                self.console.print(f"\n[dim]{'='*50}[/]")
                self.console.print(f"[bold cyan]Monitoring Cycle #{cycle_count}[/] - {datetime.datetime.now().strftime('%H:%M:%S')}")
                self.console.print(f"[dim]{'='*50}[/]")
                
                # Monitor active printers
                for manager in self.printer_managers[:]:  # Copy list to allow modification
                    if not self._monitor_printer(manager):
                        # Move to unreachable if monitoring fails
                        self.console.print(f"[red]Moving {manager.name} to unreachable list[/]")
                        self.printer_managers.remove(manager)
                        self.unreachable_printers.append(
                            (manager.printer_id, manager.name, manager.ip, 
                             manager.serial, manager.access_code)
                        )
                        manager.disconnect()
                
                # Attempt to reconnect unreachable printers
                self._retry_unreachable_printers()
                
                # Status summary
                self.console.print(f"\n[dim]Active: {len(self.printer_managers)} | Unreachable: {len(self.unreachable_printers)}[/]")
                self.console.print(f"[dim]Next check in 10 seconds...[/]")
                
                # Health check interval
                time.sleep(10)
                
            except KeyboardInterrupt:
                self.console.print("\n[yellow]Stopping monitoring...[/]")
                self.running = False
            except Exception as e:
                logging.error(f"Error in monitor loop: {e}")
                self.console.print(f"[red]Error in monitoring loop: {e}[/]")
                self.console.print("[yellow]Retrying in 30 seconds...[/]")
                time.sleep(30)  # Wait before retrying
    
    def _monitor_printer(self, manager: PrinterConnectionManager) -> bool:
        """Monitor a single printer, return False if it should be moved to unreachable."""
        try:
            # Check connection health
            if not manager.check_health():
                if manager.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    self.console.print(f"[red]{manager.name} marked as unreachable.[/]")
                    return False
                # Try to reconnect
                if not manager.reconnect():
                    return False
            
            # Get status safely
            status_data = manager.get_status_safe()
            if not status_data:
                return manager.consecutive_failures < MAX_CONSECUTIVE_FAILURES
            
            # Process and display status
            self._process_printer_status(manager, status_data)
            return True
            
        except Exception as e:
            logging.error(f"Error monitoring {manager.name}: {e}")
            return False
    
    def _retry_unreachable_printers(self):
        """Periodically retry connecting to unreachable printers."""
        current_time = time.time()
        if not self.unreachable_printers:
            return
        
        if current_time - self.last_retry_attempt_time < self.RETRY_INTERVAL_SECONDS:
            return
        
        self.last_retry_attempt_time = current_time
        self.console.print(f"[blue]Retrying {len(self.unreachable_printers)} unreachable printers...[/]")
        
        reconnected = []
        for printer_data in self.unreachable_printers:
            printer_id, name, ip, serial, access_code = printer_data
            manager = PrinterConnectionManager(
                printer_id, name, ip, serial, access_code, self.db_manager, self.mqtt_logger
            )
            
            if manager.connect():
                self.printer_managers.append(manager)
                reconnected.append(printer_data)
                self.console.print(f"[green]Reconnected to {name}[/]")
        
        # Remove reconnected printers from unreachable list
        for printer_data in reconnected:
            self.unreachable_printers.remove(printer_data)
    
    def _process_printer_status(self, manager: PrinterConnectionManager, status_data: Dict):
        """Process and display printer status."""
        try:
            # Extract status data
            status = status_data.get('status', 'UNKNOWN')
            percentage = status_data.get('percentage', 0)
            gcode_file = status_data.get('gcode_file') or 'N/A'
            layer_num = status_data.get('layer_num', 0)
            total_layer_num = status_data.get('total_layer_num', 0)
            bed_temp = status_data.get('bed_temp', 0)
            nozzle_temp = status_data.get('nozzle_temp', 0)
            remaining_time_min = status_data.get('remaining_time_min')
            
            # Format status with color
            status_color = "white"
            if status == "RUNNING": status_color = "green"
            elif status == "FINISH": status_color = "blue"
            elif status == "FAILED": status_color = "red"
            elif status == "IDLE": status_color = "yellow"
            status_str = f"[{status_color}]{status}[/]"
            
            # Format progress bar
            progress_bar = "[dim]N/A[/]"
            if isinstance(percentage, (int, float)) and 0 <= percentage <= 100:
                bar_length = 20
                filled_length = int(bar_length * percentage / 100)
                bar = '#' * filled_length + '-' * (bar_length - filled_length)
                progress_bar = f"[cyan]|{bar}| {percentage:.1f}%[/]"
            
            # Format finish time
            finish_time_str = "[dim]N/A[/]"
            if isinstance(remaining_time_min, (int, float)) and remaining_time_min >= 0:
                try:
                    finish_time = datetime.datetime.now() + datetime.timedelta(minutes=int(remaining_time_min))
                    finish_time_str = finish_time.strftime("%H:%M:%S")
                except:
                    pass
            
            # Create display table
            table = Table(show_header=False, box=None, padding=(0, 1))
            table.add_column("Attribute", style="dim", width=12)
            table.add_column("Value")
            
            self.console.print(f"\n[bold magenta]{manager.name}[/] (ID: {manager.printer_id})")
            table.add_row("Status", status_str)
            table.add_row("Progress", progress_bar)
            table.add_row("File", f"[cyan]{gcode_file}[/]")
            table.add_row("Layer", f"{layer_num}/{total_layer_num}")
            table.add_row("Temps", f"Bed: {bed_temp}°C, Nozzle: {nozzle_temp}°C")
            table.add_row("Est. Finish", finish_time_str)

            # Add tray_now info if available
            tray_now = status_data.get('tray_now')
            if tray_now is not None:
                tray_now_int = int(tray_now) if isinstance(tray_now, str) else tray_now
                if tray_now_int < 16:
                    ams_id = tray_now_int // 4
                    tray_id = tray_now_int % 4
                    table.add_row("Active Tray", f"AMS {ams_id}, Tray {tray_id}")
                elif tray_now_int in [254, 255]:
                    table.add_row("Active Tray", "External Spool")

            self.console.print(table)
            
            # Update database
            self._update_printer_database(manager, status, remaining_time_min, gcode_file, percentage)

            # Log job events (pass full status_data for filament extraction)
            self._log_job_event(manager, status, gcode_file, remaining_time_min, percentage, status_data)

            # Update manager state
            manager.previous_status = status
            manager.previous_filename = gcode_file
            
        except Exception as e:
            logging.error(f"Error processing status for {manager.name}: {e}")
    
    def _update_printer_database(self, manager, status, remaining_time_min, gcode_file, percentage):
        """Update printer status in database."""
        try:
            now = datetime.datetime.utcnow()  # Use UTC for consistent timezone handling
            remaining_seconds = None
            if isinstance(remaining_time_min, (int, float)) and remaining_time_min >= 0:
                remaining_seconds = int(remaining_time_min * 60)
            
            progress_float = None
            if isinstance(percentage, (int, float)) and 0 <= percentage <= 100:
                progress_float = float(percentage)
            
            self.db_manager.execute_query(
                """
                UPDATE printers
                SET last_poll_status = %s,
                    last_polled_at = %s,
                    remaining_time = %s,
                    current_print_job = %s,
                    Print_Progress = %s
                WHERE printer_id = %s;
                """,
                (status, now, remaining_seconds, gcode_file if gcode_file != 'N/A' else None, 
                 progress_float, manager.printer_id)
            )
        except Exception as e:
            logging.error(f"Failed to update database for {manager.name}: {e}")
    
    def _log_job_event(self, manager, status, gcode_file, remaining_time_min, percentage, status_data: Dict):
        """Log job start/end events."""
        try:
            is_running = status == "RUNNING"
            was_running = manager.previous_status == "RUNNING"

            # Check if we need to backfill filament info for a loaded ongoing job
            if manager.needs_filament_backfill and manager.current_job_id and is_running:
                self.console.print(f"  [cyan]Backfilling filament info for job {manager.current_job_id}...[/]")
                self._log_job_filaments(manager.current_job_id, manager.printer_id, status_data)
                manager.needs_filament_backfill = False
                logging.info(f"Printer {manager.name}: Backfilled filament info for job {manager.current_job_id}")

            # Job start detection
            if not was_running and is_running and gcode_file and gcode_file != 'N/A':
                self.console.print(f"  [dim]DEBUG: Detected job START (was_running={was_running}, is_running={is_running})[/]")
                self._log_job_start(manager, status, gcode_file, remaining_time_min, percentage, status_data)

            # During print: track filament usage changes
            elif is_running and was_running and manager.current_job_id:
                self.console.print(f"  [dim]DEBUG: Job RUNNING, calling _update_filament_usage (job_id={manager.current_job_id})[/]")
                self._update_filament_usage(manager.current_job_id, status_data)

            # Job end detection
            elif was_running and not is_running and manager.previous_filename and manager.previous_filename != 'N/A':
                self.console.print(f"  [dim]DEBUG: Detected job END (was_running={was_running}, is_running={is_running})[/]")
                self._log_job_end(manager, status)
            else:
                self.console.print(f"  [dim]DEBUG: No job event triggered (is_running={is_running}, was_running={was_running}, current_job_id={manager.current_job_id})[/]")

        except Exception as e:
            logging.error(f"Failed to log job event for {manager.name}: {e}")
    
    def _update_filament_usage(self, job_id: int, status_data: Dict):
        """Update was_used flags during printing as tray_now changes."""
        try:
            self.console.print(f"  [dim]DEBUG: _update_filament_usage called for job {job_id}[/]")

            # Get current tray_now
            tray_now = status_data.get('tray_now')
            self.console.print(f"  [dim]DEBUG: tray_now from status_data = {tray_now} (type: {type(tray_now)})[/]")

            if tray_now is None:
                self.console.print(f"  [yellow]DEBUG: tray_now is None, cannot update filament usage[/]")
                logging.debug(f"Job {job_id}: tray_now is None")
                return

            tray_now_int = int(tray_now) if isinstance(tray_now, str) else tray_now
            self.console.print(f"  [dim]DEBUG: tray_now_int = {tray_now_int}[/]")
            logging.debug(f"Job {job_id}: tray_now = {tray_now_int}")

            # Decode tray_now to get active AMS and tray
            if tray_now_int < 16:  # Valid AMS tray
                active_ams_id = tray_now_int // 4
                active_tray_id = tray_now_int % 4

                self.console.print(f"  [cyan]DEBUG: Decoded tray_now={tray_now_int} -> AMS {active_ams_id}, Tray {active_tray_id}[/]")
                logging.info(f"Job {job_id}: Marking AMS {active_ams_id}, Tray {active_tray_id} as used (tray_now={tray_now_int})")

                # Update was_used flag for this filament
                self.console.print(f"  [dim]DEBUG: Running UPDATE query for job_id={job_id}, ams_id={active_ams_id}, tray_id={active_tray_id}[/]")
                rows_affected = self.db_manager.execute_query(
                    """
                    UPDATE printer_job_filaments
                    SET was_used = true
                    WHERE job_history_id = %s
                      AND ams_id = %s
                      AND tray_id = %s
                      AND was_used = false
                    RETURNING ams_id, tray_id;
                    """,
                    (job_id, active_ams_id, active_tray_id),
                    fetch=True
                )

                self.console.print(f"  [dim]DEBUG: UPDATE query returned: {rows_affected}[/]")

                if rows_affected:
                    logging.info(f"Job {job_id}: Updated {len(rows_affected)} filament(s) to was_used=true")
                    # Also show in console for visibility
                    self.console.print(f"  [green][OK] Filament usage detected:[/] AMS {active_ams_id}, Tray {active_tray_id} now marked as USED")
                else:
                    self.console.print(f"  [yellow]DEBUG: No rows updated (already marked as used, or filament not found)[/]")
            elif tray_now_int in [254, 255]:
                self.console.print(f"  [dim]DEBUG: tray_now={tray_now_int} is external spool (already marked as used at job start)[/]")
                logging.debug(f"Job {job_id}: tray_now={tray_now_int} is external spool")
            else:
                self.console.print(f"  [yellow]DEBUG: tray_now={tray_now_int} is unexpected value[/]")
            # For external spool (255/254), it's already marked as used at job start

        except Exception as e:
            self.console.print(f"  [red]DEBUG: Exception in _update_filament_usage: {e}[/]")
            logging.error(f"Failed to update filament usage for job {job_id}: {e}")

    def _extract_filament_info(self, status_data: Dict) -> Optional[Dict]:
        """Extract and process filament information from status data."""
        try:
            vt_tray = status_data.get('vt_tray')
            if not vt_tray:
                return None

            # Extract filament information
            tray_type = getattr(vt_tray, 'tray_type', None)
            tray_color_raw = getattr(vt_tray, 'tray_color', None)

            # Remove FF suffix if present (8-char hex -> 6-char hex)
            tray_color = None
            if tray_color_raw and tray_color_raw not in ['N/A', ''] and len(tray_color_raw) == 8 and tray_color_raw.endswith('FF'):
                tray_color = tray_color_raw[:-2]
            elif tray_color_raw and tray_color_raw not in ['N/A', '']:
                tray_color = tray_color_raw

            tray_weight = getattr(vt_tray, 'tray_weight', None)
            tray_brand = getattr(vt_tray, 'tray_sub_brands', None)
            tray_info_idx = getattr(vt_tray, 'tray_info_idx', None)
            tray_diameter = getattr(vt_tray, 'tray_diameter', None)
            tray_uuid = getattr(vt_tray, 'tray_uuid', None)
            bed_temp = getattr(vt_tray, 'bed_temp', None)

            # Convert temperature values
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
                pass

            # Look up filament information from database if available
            db_info = None
            if tray_info_idx and tray_info_idx not in ['N/A', '']:
                try:
                    result = self.db_manager.execute_query(
                        """
                        SELECT name, material_type, vendor, nozzle_temp_min, nozzle_temp_max,
                               bed_temp, density, cost, diameter
                        FROM bambu_filament_profiles
                        WHERE filament_id = %s
                        """,
                        (tray_info_idx,),
                        fetch=True
                    )
                    if result and len(result) > 0:
                        db_info = {
                            'db_name': result[0][0],
                            'db_material_type': result[0][1],
                            'db_vendor': result[0][2],
                            'db_temp_min': result[0][3],
                            'db_temp_max': result[0][4],
                            'db_bed_temp': result[0][5],
                            'db_density': result[0][6],
                            'db_cost': result[0][7],
                            'db_diameter': result[0][8]
                        }
                except Exception:
                    pass

            # Only create filament info if we have meaningful data
            if tray_type and tray_type not in ['N/A', ''] and (temp_min > 0 or temp_max > 0 or db_info or (tray_color and tray_color not in ['000000', '00000000'])):
                return {
                    'type': tray_type,
                    'color': tray_color,
                    'temp_min': temp_min,
                    'temp_max': temp_max,
                    'bed_temp': bed_temp_int,
                    'weight': tray_weight,
                    'brand': tray_brand,
                    'diameter': tray_diameter,
                    'tray_info_idx': tray_info_idx,
                    'tray_uuid': tray_uuid,
                    'db_info': db_info
                }

            return None

        except Exception as e:
            logging.error(f"Error extracting filament info: {e}")
            return None

    def _log_job_filaments(self, job_id: int, printer_id: int, status_data: Dict):
        """Save ALL filament information for this print job (supports multi-color prints)."""
        if not job_id:
            return

        try:
            filaments_captured = []
            filaments_used = []

            # Get the currently active filament (vt_tray)
            active_filament_info = self._extract_filament_info(status_data)
            active_tray_uuid = active_filament_info.get('tray_uuid') if active_filament_info else None

            # Get tray_now to identify which filament is actually being used
            # tray_now format: integer where value encodes AMS unit and tray position
            # Formula: tray_now = (ams_id * 4) + tray_id
            # Example: tray_now=5 means AMS 1, Tray 1 (5 = 1*4 + 1)
            # Special: tray_now=255 or tray_now=254 means external spool
            tray_now = status_data.get('tray_now')

            # DEBUG: Show tray_now at job start
            self.console.print(f"  [dim]DEBUG _log_job_filaments: tray_now = {tray_now} (type: {type(tray_now)})[/]")

            active_ams_id = None
            active_tray_id = None

            if tray_now is not None:
                tray_now_int = int(tray_now) if isinstance(tray_now, str) else tray_now
                if tray_now_int < 16:  # Valid AMS tray (0-15 covers 4 AMS units * 4 trays)
                    active_ams_id = tray_now_int // 4
                    active_tray_id = tray_now_int % 4
                # else: external spool (255 or 254)

            # Get AMS hub data for all loaded filaments
            ams_hub = status_data.get('ams_hub')

            if ams_hub:
                # Printer has AMS - capture all loaded filaments
                for ams_id in range(4):  # Check up to 4 AMS units
                    try:
                        ams = ams_hub[ams_id]
                        # Get all 4 tray positions in this AMS
                        for tray_id in range(4):
                            tray = ams.get_filament_tray(tray_id)
                            if tray:
                                tray_info_idx = getattr(tray, 'tray_info_idx', None)
                                tray_uuid = getattr(tray, 'tray_uuid', None)

                                # Look up database info
                                db_info = None
                                if tray_info_idx and tray_info_idx not in ['N/A', '']:
                                    try:
                                        result = self.db_manager.execute_query(
                                            """
                                            SELECT name, material_type, vendor, nozzle_temp_min, nozzle_temp_max,
                                                   bed_temp, density, cost, diameter
                                            FROM bambu_filament_profiles
                                            WHERE filament_id = %s
                                            """,
                                            (tray_info_idx,),
                                            fetch=True
                                        )
                                        if result and len(result) > 0:
                                            db_info = {
                                                'db_name': result[0][0],
                                                'db_vendor': result[0][2],
                                                'db_temp_min': result[0][3],
                                                'db_temp_max': result[0][4],
                                                'db_bed_temp': result[0][5],
                                                'db_density': result[0][6],
                                                'db_cost': result[0][7],
                                                'db_diameter': result[0][8]
                                            }
                                    except Exception:
                                        pass

                                # Extract tray color
                                tray_color_raw = getattr(tray, 'tray_color', None)
                                tray_color = None
                                if tray_color_raw and tray_color_raw not in ['N/A', ''] and len(tray_color_raw) == 8 and tray_color_raw.endswith('FF'):
                                    tray_color = tray_color_raw[:-2]
                                elif tray_color_raw and tray_color_raw not in ['N/A', '']:
                                    tray_color = tray_color_raw

                                # Check if this is the primary (active) filament
                                is_primary = (tray_uuid == active_tray_uuid) if active_tray_uuid else False

                                # Check if this filament was actually used (matches tray_now)
                                was_used = (ams_id == active_ams_id and tray_id == active_tray_id)

                                # DEBUG: Show what was_used is being set to
                                self.console.print(f"  [dim]DEBUG: AMS {ams_id} Tray {tray_id}: was_used={was_used} (active_ams_id={active_ams_id}, active_tray_id={active_tray_id})[/]")

                                # Insert filament record
                                self.db_manager.execute_query(
                                    """
                                    INSERT INTO printer_job_filaments (
                                        job_history_id, printer_id, filament_id, tray_uuid,
                                        ams_id, tray_id, is_primary, was_used,
                                        filament_name, filament_type, filament_color,
                                        filament_vendor, temp_min, temp_max, bed_temp,
                                        weight, cost, density, diameter
                                    ) VALUES (
                                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                                    )
                                    ON CONFLICT (job_history_id, ams_id, tray_id) DO NOTHING;
                                    """,
                                    (
                                        job_id, printer_id,
                                        tray_info_idx,
                                        tray_uuid,
                                        ams_id, tray_id, is_primary, was_used,
                                        db_info.get('db_name') if db_info else None,
                                        getattr(tray, 'tray_type', None),
                                        tray_color,
                                        db_info.get('db_vendor') if db_info else getattr(tray, 'tray_sub_brands', None),
                                        getattr(tray, 'nozzle_temp_min', None),
                                        getattr(tray, 'nozzle_temp_max', None),
                                        getattr(tray, 'bed_temp', None),
                                        getattr(tray, 'tray_weight', None),
                                        db_info.get('db_cost') if db_info else None,
                                        db_info.get('db_density') if db_info else None,
                                        db_info.get('db_diameter') if db_info else getattr(tray, 'tray_diameter', None)
                                    )
                                )

                                filament_type = db_info.get('db_name') if db_info and db_info.get('db_name') else getattr(tray, 'tray_type', 'Unknown')
                                filament_desc = f"{filament_type} ({tray_color or 'no color'})"
                                filaments_captured.append(filament_desc)
                                if was_used:
                                    filaments_used.append(filament_desc)

                    except KeyError:
                        # AMS unit doesn't exist
                        continue

            else:
                # No AMS - capture single external spool filament
                if active_filament_info:
                    db_info = active_filament_info.get('db_info', {}) or {}

                    # External spool is always considered "used" if it's active
                    was_used = True

                    self.db_manager.execute_query(
                        """
                        INSERT INTO printer_job_filaments (
                            job_history_id, printer_id, filament_id, tray_uuid,
                            ams_id, tray_id, is_primary, was_used,
                            filament_name, filament_type, filament_color,
                            filament_vendor, temp_min, temp_max, bed_temp,
                            weight, cost, density, diameter
                        ) VALUES (
                            %s, %s, %s, %s, NULL, NULL, true, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (job_history_id, ams_id, tray_id) DO NOTHING;
                        """,
                        (
                            job_id, printer_id,
                            active_filament_info.get('tray_info_idx'),
                            active_filament_info.get('tray_uuid'),
                            was_used,
                            db_info.get('db_name'),
                            active_filament_info.get('type'),
                            active_filament_info.get('color'),
                            db_info.get('db_vendor') if db_info else active_filament_info.get('brand'),
                            active_filament_info.get('temp_min'),
                            active_filament_info.get('temp_max'),
                            active_filament_info.get('bed_temp'),
                            active_filament_info.get('weight'),
                            db_info.get('db_cost'),
                            db_info.get('db_density'),
                            db_info.get('db_diameter') if db_info else active_filament_info.get('diameter')
                        )
                    )

                    filament_type = db_info.get('db_name') if db_info and db_info.get('db_name') else active_filament_info.get('type', 'Unknown')
                    filament_desc = f"{filament_type} ({active_filament_info.get('color', 'no color')})"
                    filaments_captured.append(filament_desc)
                    filaments_used.append(filament_desc)

            # Display captured filaments with detailed info
            if filaments_captured:
                # Show tray_now debug info
                tray_now_display = "None"
                if tray_now is not None:
                    tray_now_int = int(tray_now) if isinstance(tray_now, str) else tray_now
                    if tray_now_int < 16:
                        tray_now_display = f"{tray_now_int} (AMS {active_ams_id}, Tray {active_tray_id})"
                    else:
                        tray_now_display = f"{tray_now_int} (External)"

                self.console.print(f"  [dim]tray_now: {tray_now_display}[/]")
                self.console.print(f"  [green]Filaments loaded ({len(filaments_captured)}):[/] {', '.join(filaments_captured)}")
                if filaments_used:
                    self.console.print(f"  [cyan]Filaments actively used ({len(filaments_used)}):[/] {', '.join(filaments_used)}")
                else:
                    self.console.print(f"  [yellow]No filaments marked as used yet (tray_now may not be set)[/]")
            else:
                self.console.print(f"  [yellow]No filament data captured[/]")

        except Exception as e:
            logging.error(f"Failed to log filaments for job {job_id}: {e}")

    def _log_job_start(self, manager, status, gcode_file, remaining_time_min, percentage, status_data: Dict):
        """Log job start event."""
        now = datetime.datetime.utcnow()  # Use UTC for consistent timezone handling

        # Check for existing unfinished job
        existing = self.db_manager.execute_query(
            """
            SELECT COUNT(*) FROM printer_job_history
            WHERE printer_id = %s AND filename = %s AND end_time IS NULL;
            """,
            (manager.printer_id, gcode_file),
            fetch=True
        )

        if existing and existing[0][0] == 0:
            # Calculate estimated start time
            actual_start_time = now
            interval_string = None

            if isinstance(remaining_time_min, (int, float)) and remaining_time_min >= 0:
                remaining_seconds = int(remaining_time_min * 60)
                if isinstance(percentage, (int, float)) and 0 < percentage < 100:
                    try:
                        estimated_total_seconds = int(float(remaining_seconds) * 100.0 / (100.0 - float(percentage)))
                        interval_string = f"{estimated_total_seconds} seconds"
                        elapsed_seconds = (float(percentage) * float(remaining_seconds)) / (100.0 - float(percentage))
                        actual_start_time = now - datetime.timedelta(seconds=elapsed_seconds)
                    except:
                        interval_string = f"{remaining_seconds} seconds"
                else:
                    interval_string = f"{remaining_seconds} seconds"

            # Insert job start
            result = self.db_manager.execute_query(
                """
                INSERT INTO printer_job_history (printer_id, filename, start_time, status, total_print_time)
                VALUES (%s, %s, %s, %s, %s::interval) RETURNING id;
                """,
                (manager.printer_id, gcode_file, actual_start_time, status, interval_string),
                fetch=True
            )

            if result:
                manager.current_job_id = result[0][0]
                self.console.print(f"  [green]Job START:[/] {gcode_file} (ID: {manager.current_job_id})")

                # Capture ALL filament information (supports multi-color prints)
                self._log_job_filaments(manager.current_job_id, manager.printer_id, status_data)
    
    def _log_job_end(self, manager, status):
        """Log job end event."""
        now = datetime.datetime.utcnow()  # Use UTC for consistent timezone handling
        
        rows_updated = self.db_manager.execute_query(
            """
            UPDATE printer_job_history
            SET end_time = %s, status = %s
            WHERE printer_id = %s AND filename = %s AND end_time IS NULL;
            """,
            (now, status, manager.printer_id, manager.previous_filename)
        )
        
        if rows_updated > 0:
            self.console.print(f"  [blue]Job END:[/] {manager.previous_filename} - {status}")
            if status == "FINISH":
                self.console.print(f"  [green]Job completed successfully[/]")
            manager.current_job_id = None
    
    def shutdown(self):
        """Clean shutdown of all connections."""
        self.running = False
        self.console.print("[cyan]Shutting down...[/]")
        
        for manager in self.printer_managers:
            manager.disconnect()
        
        self.db_manager.close()
        self.console.print("[green]Shutdown complete.[/]")

if __name__ == '__main__':
    monitor = SafePrinterMonitor()
    try:
        monitor.initialize_printers()
        monitor.monitor_loop()
    finally:
        monitor.shutdown()
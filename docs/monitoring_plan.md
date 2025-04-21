# Bambulabs Printer Monitoring Script Plan

This document outlines the plan for creating a Python script to monitor Bambu Lab printers using the `bambulabs_api` library and fetching printer details from a PostgreSQL database.

## Requirements

*   Monitor multiple Bambu Lab printers.
*   Retrieve printer connection details (IP, Serial, Access Code) from the `familyRewardDb` database, `printers` table.
*   Use the `bambulabs_api` library to connect and get printer status.
*   Handle database credentials securely using environment variables.
*   Provide clear setup and execution instructions.

## Database Schema (`printers` table)

The required columns identified are:
*   `printer_ip` (character varying)
*   `printer_bambu_id` (character varying) - Corresponds to the Serial Number
*   `access_code` (character varying)
*   `printer_name` (character varying) - For identifying printers in output

## Plan Details

1.  **Add Dependency:**
    *   Add `psycopg2-binary` to `requirements.txt` to enable PostgreSQL connection.

2.  **Create Monitoring Script (`examples/monitor_printers.py`):**
    *   **Import Libraries:** `bambulabs_api`, `psycopg2`, `os`.
    *   **Database Credentials:** Read `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` from environment variables.
    *   **Connect to Database:** Use `psycopg2.connect()` with the credentials.
    *   **Fetch Printer Data:** Execute `SELECT printer_name, printer_ip, printer_bambu_id, access_code FROM printers;`.
    *   **Iterate and Monitor:**
        *   Loop through fetched printer records.
        *   Instantiate `bambulabs_api.Printer(ip, access_code, serial)`.
        *   `printer.connect()`.
        *   `status = printer.get_state()`.
        *   Print `f"{printer_name}: {status}"`.
        *   `printer.disconnect()`.
    *   **Error Handling:** Implement `try...except` blocks for database and printer connection/communication errors.
    *   **Close Connection:** Use a `finally` block or context manager (`with`) to ensure the database connection is closed.

3.  **Setup and Execution:**
    *   **Install Dependency:** `pip install -r requirements.txt`.
    *   **Set Environment Variables:**
        ```bash
        export DB_HOST=192.168.7.23
        export DB_PORT=5432
        export DB_NAME=familyRewardDb
        export DB_USER=postgres
        export DB_PASSWORD='YourPasswordHere' # Replace with actual password
        ```
        *(Note: On Windows, use `set` instead of `export`)*
    *   **Run Script:** `python examples/monitor_printers.py`.

## Workflow Diagram

```mermaid
flowchart TD
    Start --> SetEnvVars[Set DB Environment Variables]
    SetEnvVars --> InstallDeps[Install Dependencies (pip install -r requirements.txt)]
    InstallDeps --> RunScript[Run monitor_printers.py]
    RunScript --> ConnectDB[Connect to PostgreSQL]
    ConnectDB --> QueryPrinters[Query 'printers' table]
    QueryPrinters --> LoopPrinters{Loop through each printer}
    LoopPrinters -->|Next Printer| InstantiateAPI[Instantiate bambulabs_api.Printer]
    InstantiateAPI --> ConnectPrinter[Connect to Printer]
    ConnectPrinter --> GetStatus[Get Printer Status]
    GetStatus --> PrintStatus[Print Status]
    PrintStatus --> DisconnectPrinter[Disconnect Printer]
    DisconnectPrinter --> LoopPrinters
    LoopPrinters -->|Done| CloseDB[Close DB Connection]
    CloseDB --> End

    ConnectDB -->|Error| HandleDBError[Handle DB Error]
    ConnectPrinter -->|Error| HandlePrinterError[Handle Printer Error]
    GetStatus -->|Error| HandlePrinterError
    HandleDBError --> End
    HandlePrinterError --> DisconnectPrinter -- Maybe skip disconnect --> LoopPrinters
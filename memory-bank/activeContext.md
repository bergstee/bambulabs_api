# Active Context: Remote Print Initiation API Implementation

## 1. Current Focus

The current focus is on implementing the REST API endpoint for remote print initiation within the `bambulabs_api` application.

## 2. Recent Changes & Decisions

*   **Communication Mechanism:** A RESTful API (`POST /api/v1/print_jobs`) was designed and has now been implemented.
*   **Payload Defined:** Key fields include `printer_id` and `file_path`, with support for filament/AMS mapping parameters (`use_ams` and `ams_mapping`).
*   **Security:** API Key authentication (`X-API-Key` header) and HTTPS were recommended in the design. Basic API key validation is implemented in the new endpoint.
*   **Framework Selection:** FastAPI was chosen as the web framework for its performance and features.
*   **Endpoint Implementation:** The `POST /api/v1/print_jobs` endpoint has been implemented in `bambulabs_api/server/main.py`.
*   **Dependencies:** `fastapi` and `uvicorn` have been added to `requirements.txt`.
*   **Basic Command Handling:** The endpoint includes logic to parse the incoming command, perform basic validation (API key, file existence, file type), and simulate initiating a print (in development mode) or use the `bambulabs_api` client to actually initiate a print (in production mode).
*   **Error Handling:** Basic error handling for invalid input, authentication failures, and internal server errors is included.
*   **Documentation:** The initial design was documented in the memory bank files. This update reflects the start of the implementation phase.

## 3. Next Steps

*   **Refine Printer Identification:** Implement a proper mechanism to map the `printer_id` from the request payload to an actual `bambulabs_api.Printer` instance, especially if managing multiple printers. This might involve a configuration file or a discovery mechanism.
*   **Enhance File Path Security:** Implement more robust validation and sanitization for the `file_path` to prevent security vulnerabilities. Consider allowing only specific directories.
*   **Implement `print_parameters` Handling:** Define and implement how the optional `print_parameters` object in the payload will be used by the underlying print initiation logic.
*   **Implement Logging:** Add more detailed logging for API requests, validation failures, and print initiation outcomes.
*   **Add Configuration:** Implement a proper configuration loading mechanism for API settings (host, port, API key, printer details).
*   **Add Tests:** Write unit and integration tests for the API endpoint.
*   **HTTPS Setup:** Document or implement how to run the API server with HTTPS.

## 4. Active Considerations & Open Questions

*   **Printer Identification Strategy:** How will the mapping of `printer_id` to printer instance be managed? Configuration file, database, or dynamic discovery?
*   **Allowed File Paths:** What directories should be allowed for print files? Should this be configurable?
*   **`print_parameters` Structure:** What specific parameters should the `print_parameters` object support?
*   **Production Deployment:** How will the FastAPI application be deployed in a production environment (e.g., using Gunicorn with Uvicorn workers, or a systemd service)?
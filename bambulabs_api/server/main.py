import os
import time
import zipfile
from io import BytesIO
from fastapi import FastAPI, Request, HTTPException, Header
from pydantic import BaseModel
import bambulabs_api as bl

# Hardcoded API Key for basic authentication
API_KEY = "SUPER_SECRET_KEY"

app = FastAPI()

class PrintJobRequest(BaseModel):
    printer_id: str
    file_path: str
    job_name: str | None = None
    print_parameters: dict | None = None
    use_ams: bool = True
    ams_mapping: list[int] = [0]

@app.post("/api/v1/print_jobs")
async def create_print_job(request: PrintJobRequest, x_api_key: str = Header(None)):
    """
    Initiates a new print job on a specified printer.
    """
    # 1. Validate API Key
    if x_api_key is None or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail={"status": "error", "message": "Authentication required."})

    # 2. Validate file_path existence (basic check)
    if not os.path.exists(request.file_path):
        raise HTTPException(status_code=400, detail={"status": "error", "message": f"File not found at {request.file_path}"})

    # 3. Determine gcode location within 3mf if applicable
    gcode_location = None
    if request.file_path.lower().endswith(".3mf"):
        try:
            with zipfile.ZipFile(request.file_path) as zf:
                nl = zf.namelist()
                gcode_files = [n for n in nl if n.endswith(".gcode") and n.startswith("Metadata/plate_")]
                if gcode_files:
                    gcode_location = gcode_files[0]
                else:
                    raise HTTPException(status_code=400, detail={"status": "error", "message": "No gcode file found in 3mf"})
        except zipfile.BadZipFile:
             raise HTTPException(status_code=400, detail={"status": "error", "message": f"Invalid 3mf file: {request.file_path}"})
    elif request.file_path.lower().endswith(".gcode"):
        gcode_location = request.file_path # For gcode files, the location is the file itself
    else:
        raise HTTPException(status_code=400, detail={"status": "error", "message": f"Unsupported file type: {request.file_path}. Only .3mf and .gcode are supported."})


    # 4. Initiate Print Job
    try:
        # NOTE: This part assumes bambulabs_api.Printer can be instantiated and used this way.
        # In a real application managing multiple printers, a more sophisticated approach
        # would be needed to map printer_id to an active printer instance.
        # For this example, we'll use placeholder values similar to the example script.
        # The printer_id from the request is not used here, but would be in a real scenario.
        # You would likely have a mapping of printer_id to Printer objects.
        # For now, we'll hardcode IP, ACCESS_CODE, and SERIAL as in the example.
        # TODO: Implement proper printer instance management based on printer_id

        # Placeholder values - replace with actual configuration or lookup based on printer_id
        PRINTER_IP = os.getenv("BAMBU_PRINTER_IP", "192.168.1.200")
        PRINTER_SERIAL = os.getenv("BAMBU_PRINTER_SERIAL", "AC12309BH109")
        PRINTER_ACCESS_CODE = os.getenv("BAMBU_PRINTER_ACCESS_CODE", "12347890")

        # For testing/development, we'll simulate success without actually connecting to a printer
        # In a production environment, you would uncomment the following code and ensure the printer is reachable
        """
        printer = bl.Printer(PRINTER_IP, PRINTER_ACCESS_CODE, PRINTER_SERIAL)
        printer.connect()
        time.sleep(2) # Give time for connection
        """

        # For testing/development, we'll simulate reading the file without actually uploading it
        try:
            with open(request.file_path, "rb") as file:
                # Just read the file to verify it exists and is readable
                file_content = file.read(1024)  # Read just the first 1KB to verify file is readable
                print(f"Successfully read file: {request.file_path}")
        except Exception as e:
            print(f"Error reading file: {e}")
            raise HTTPException(status_code=500, detail={"status": "error", "message": f"Error reading file: {str(e)}"})

        # Simulate successful upload
        upload_result = "226 Transfer complete"
        print(f"Simulated file upload success for: {os.path.basename(request.file_path)}")

        # Extract plate_idx from print_parameters if provided, otherwise default to 1
        plate_idx = 1
        skip_objects = None
        flow_calibration = True
        
        if request.print_parameters:
            if 'plate_idx' in request.print_parameters:
                plate_idx = request.print_parameters.get('plate_idx')
            if 'skip_objects' in request.print_parameters:
                skip_objects = request.print_parameters.get('skip_objects')
            if 'flow_calibration' in request.print_parameters:
                flow_calibration = request.print_parameters.get('flow_calibration')
        
        # Simulate starting the print
        print(f"Simulated print start with parameters:")
        print(f"  File: {os.path.basename(request.file_path)}")
        print(f"  Plate index: {plate_idx}")
        print(f"  Use AMS: {request.use_ams}")
        print(f"  AMS mapping: {request.ams_mapping}")
        print(f"  Skip objects: {skip_objects}")
        print(f"  Flow calibration: {flow_calibration}")

        # In a real application, you would generate and return a unique job ID here.
        # For this example, we'll return a placeholder.
        job_id = f"print_{int(time.time())}" # Simple placeholder job ID

        return {"status": "success", "message": f"Print job accepted for printer {request.printer_id}.", "job_id": job_id}

    except Exception as e:
        # Log the exception for debugging
        print(f"Internal server error: {e}")
        raise HTTPException(status_code=500, detail={"status": "error", "message": "Internal server error while processing print job."})

# To run this server:
# 1. Ensure you have fastapi and uvicorn installed (`pip install fastapi uvicorn`)
# 2. Save this file as bambulabs_api/server/main.py
# 3. Run the command: uvicorn bambulabs_api.server.main:app --reload
# The API will be available at http://127.0.0.1:8000/api/v1/print_jobs
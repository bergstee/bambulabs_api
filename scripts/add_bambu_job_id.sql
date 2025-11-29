-- Migration: Add bambu_job_id column to printer_job_history table
-- This column stores Bambu's unique job ID from MQTT data
-- It persists across pause/resume and is the authoritative identifier for a print job

-- Add the column (BIGINT to handle large job IDs like 587508594)
ALTER TABLE printer_job_history
ADD COLUMN IF NOT EXISTS bambu_job_id BIGINT;

-- Create an index for fast lookups by bambu_job_id
CREATE INDEX IF NOT EXISTS idx_printer_job_history_bambu_job_id
ON printer_job_history(bambu_job_id);

-- Add a unique constraint to prevent duplicate job entries
-- (a given Bambu job_id should only appear once in the history)
-- Note: Using a partial index to allow NULL values (for legacy records)
CREATE UNIQUE INDEX IF NOT EXISTS idx_printer_job_history_bambu_job_id_unique
ON printer_job_history(bambu_job_id)
WHERE bambu_job_id IS NOT NULL;

-- Add comment to document the column
COMMENT ON COLUMN printer_job_history.bambu_job_id IS 'Unique job identifier from Bambu MQTT data (persists across pause/resume)';

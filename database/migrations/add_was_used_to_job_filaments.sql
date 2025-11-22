-- Add was_used column to track which filaments were actually consumed during the print
-- This uses the tray_now field from MQTT data to identify active filaments

ALTER TABLE printer_job_filaments
    ADD COLUMN IF NOT EXISTS was_used BOOLEAN DEFAULT false;

-- Add comment
COMMENT ON COLUMN printer_job_filaments.was_used IS 'True if this filament was actively used during the print (based on tray_now field). For multi-color prints, multiple filaments can be marked as used.';

-- Create an index for filtering by used filaments
CREATE INDEX IF NOT EXISTS idx_job_filaments_used
    ON printer_job_filaments(job_history_id, was_used)
    WHERE was_used = true;

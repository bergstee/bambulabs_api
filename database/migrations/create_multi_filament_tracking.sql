-- Enhanced Multi-Filament Tracking System
-- This replaces the single filament capture with support for multiple filaments per job

-- Drop the unique constraint on printer_job_filaments to allow multiple filaments per job
ALTER TABLE printer_job_filaments DROP CONSTRAINT IF EXISTS unique_job_filament;

-- Add AMS position tracking
ALTER TABLE printer_job_filaments
    ADD COLUMN IF NOT EXISTS ams_id INTEGER,
    ADD COLUMN IF NOT EXISTS tray_id INTEGER;

-- Add a flag to indicate which filament was initially active
ALTER TABLE printer_job_filaments
    ADD COLUMN IF NOT EXISTS is_primary BOOLEAN DEFAULT false;

-- Add a unique constraint on job + ams + tray (one record per tray position)
ALTER TABLE printer_job_filaments
    ADD CONSTRAINT unique_job_ams_tray UNIQUE(job_history_id, ams_id, tray_id);

-- Create an index for faster lookups
CREATE INDEX IF NOT EXISTS idx_job_filaments_ams_tray
    ON printer_job_filaments(job_history_id, ams_id, tray_id);

-- Add comments
COMMENT ON COLUMN printer_job_filaments.ams_id IS 'AMS unit ID (0-3), NULL for external spool';
COMMENT ON COLUMN printer_job_filaments.tray_id IS 'Tray ID within AMS (0-3), NULL for external spool';
COMMENT ON COLUMN printer_job_filaments.is_primary IS 'True if this was the initially active filament when job started';

COMMENT ON TABLE printer_job_filaments IS
'Captures all filaments loaded in the printer when a job starts. Supports multi-color prints by storing one record per AMS tray position. The is_primary flag indicates which filament was active at job start.';

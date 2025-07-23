-- Create table for storing Bambu Lab filament profiles
-- This table stores comprehensive filament information fetched from BambuStudio GitHub repository

CREATE TABLE IF NOT EXISTS bambu_filament_profiles (
    id SERIAL PRIMARY KEY,
    filename VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    filament_id VARCHAR(50) UNIQUE NOT NULL,
    type VARCHAR(50),
    inherits VARCHAR(100),
    from_source VARCHAR(50),
    vendor VARCHAR(100),
    cost DECIMAL(10,2),
    density DECIMAL(10,4),
    flow_ratio DECIMAL(10,4),
    material_type VARCHAR(50),
    nozzle_temp_min INTEGER,
    nozzle_temp_max INTEGER,
    bed_temp INTEGER,
    bed_temp_initial INTEGER,
    impact_strength_z DECIMAL(10,2),
    diameter DECIMAL(10,3) DEFAULT 1.75,
    retraction_length DECIMAL(10,2),
    retraction_speed DECIMAL(10,2),
    print_speed DECIMAL(10,2),
    start_gcode TEXT,
    end_gcode TEXT,
    raw_json JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes for better performance
CREATE INDEX IF NOT EXISTS idx_bambu_filament_profiles_filament_id ON bambu_filament_profiles(filament_id);
CREATE INDEX IF NOT EXISTS idx_bambu_filament_profiles_material_type ON bambu_filament_profiles(material_type);
CREATE INDEX IF NOT EXISTS idx_bambu_filament_profiles_vendor ON bambu_filament_profiles(vendor);
CREATE INDEX IF NOT EXISTS idx_bambu_filament_profiles_name ON bambu_filament_profiles(name);

-- Create a trigger to automatically update the updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_bambu_filament_profiles_updated_at 
    BEFORE UPDATE ON bambu_filament_profiles 
    FOR EACH ROW 
    EXECUTE FUNCTION update_updated_at_column();

-- Add comments to document the table structure
COMMENT ON TABLE bambu_filament_profiles IS 'Stores Bambu Lab filament profiles fetched from BambuStudio GitHub repository';
COMMENT ON COLUMN bambu_filament_profiles.filament_id IS 'Unique filament identifier (e.g., GFA01, GFB00)';
COMMENT ON COLUMN bambu_filament_profiles.material_type IS 'Basic material type (PLA, ABS, PETG, TPU, etc.)';
COMMENT ON COLUMN bambu_filament_profiles.nozzle_temp_min IS 'Minimum recommended nozzle temperature in Celsius';
COMMENT ON COLUMN bambu_filament_profiles.nozzle_temp_max IS 'Maximum recommended nozzle temperature in Celsius';
COMMENT ON COLUMN bambu_filament_profiles.bed_temp IS 'Recommended bed temperature in Celsius';
COMMENT ON COLUMN bambu_filament_profiles.raw_json IS 'Complete original JSON data from GitHub for reference';
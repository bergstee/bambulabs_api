-- Update the stock transaction trigger to support item variations
-- This will check if an item has color variations and match them with the actual filaments used
-- Supports multi-color prints by checking if ANY used filament matches the variation

CREATE OR REPLACE FUNCTION public.record_stock_on_print_finish()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    item_record RECORD;
    notes_text TEXT;
    filament_info_text TEXT;
    matched_variation_id INTEGER;
BEGIN
    -- Check if the status is updated to 'FINISH' and end_time is newly set
    IF NEW.status = 'FINISH' AND NEW.end_time IS NOT NULL AND OLD.end_time IS NULL THEN
        RAISE NOTICE 'Trigger fired for job ID: %, Filename: %', NEW.id, NEW.filename;

        -- Build base notes text
        notes_text := 'Print completed on printer ID ' || NEW.printer_id::text;

        -- Try to get filament information from printer_job_filaments table
        BEGIN
            SELECT
                CASE
                    WHEN filament_name IS NOT NULL THEN filament_name
                    WHEN filament_type IS NOT NULL THEN filament_type
                    ELSE 'Unknown'
                END ||
                CASE
                    WHEN filament_color IS NOT NULL THEN ' (Color: ' || filament_color || ')'
                    ELSE ''
                END ||
                CASE
                    WHEN filament_vendor IS NOT NULL THEN ' - ' || filament_vendor
                    ELSE ''
                END
            INTO filament_info_text
            FROM printer_job_filaments
            WHERE job_history_id = NEW.id
              AND was_used = true  -- Only get the filament that was actually used
            LIMIT 1;  -- In case multiple colors were used, take the first one

            -- Add filament info to notes if found
            IF filament_info_text IS NOT NULL THEN
                notes_text := notes_text || ' | Filament: ' || filament_info_text;
            END IF;
        EXCEPTION
            WHEN OTHERS THEN
                -- If table doesn't exist yet or any error, just skip filament info
                RAISE NOTICE 'Could not retrieve filament info for job %', NEW.id;
        END;

        -- Loop through items associated with the finished print file
        FOR item_record IN
            SELECT pfm.item_id, pfm.quantity, pfm.item_subassembly_id
            FROM printer_file_models pfm
            JOIN printer_files pf ON pfm.printer_file_id = pf.id
            WHERE pf.filename = NEW.filename
        LOOP
            IF item_record.item_id IS NOT NULL AND item_record.quantity IS NOT NULL THEN
                RAISE NOTICE '  Processing item_id: %, quantity: %', item_record.item_id, item_record.quantity;

                -- Initialize variation_id as NULL (for items without variations)
                matched_variation_id := NULL;

                -- Check if this item has color variations
                IF EXISTS (
                    SELECT 1 FROM items_with_variations
                    WHERE item_id = item_record.item_id
                      AND variation_name = 'color'
                      AND is_active = true
                ) THEN
                    RAISE NOTICE '  Item has color variations, attempting to match with used filaments';

                    -- Try to find matching variation by checking if ANY of the used filaments
                    -- match the color variation mapping
                    SELECT iwv.id INTO matched_variation_id
                    FROM items_with_variations iwv
                    JOIN color_variation_mapping cvm ON iwv.id = cvm.items_with_variations_id
                    JOIN printer_job_filaments pjf ON pjf.filament_color = cvm.filament_color_hex
                    WHERE iwv.item_id = item_record.item_id
                      AND iwv.variation_name = 'color'
                      AND iwv.is_active = true
                      AND cvm.is_active = true
                      AND pjf.job_history_id = NEW.id
                      AND pjf.was_used = true  -- Only check filaments that were actually used
                    LIMIT 1;  -- Take first match if multiple filaments match

                    IF matched_variation_id IS NOT NULL THEN
                        RAISE NOTICE '  Matched to variation_id: %', matched_variation_id;
                    ELSE
                        RAISE WARNING '  No matching color variation found for item_id: % with any used filament',
                            item_record.item_id;
                        -- Could optionally skip this transaction or log an error
                        -- For now, we'll create the transaction without a variation_id
                    END IF;
                ELSE
                    RAISE NOTICE '  Item has no color variations, using base item stock';
                END IF;

                -- Insert into stock_transactions with variation_id if matched
                INSERT INTO stock_transactions
                    (item_id, quantity, transaction_type, transaction_date, notes,
                     print_job_id, item_assembly_id, item_with_variation_id)
                VALUES
                    (item_record.item_id, item_record.quantity, 'PRINT_COMPLETE',
                     NEW.end_time, notes_text, NEW.id, item_record.item_subassembly_id,
                     matched_variation_id);

                RAISE NOTICE '  Inserted stock transaction for item_id: %, variation_id: %',
                    item_record.item_id, matched_variation_id;

                -- Update filament quantities
                -- This will reduce the current_filament_grams for each filament used by this item
                MERGE INTO filaments f
                USING (
                    SELECT
                        if2.filament_id,
                        abs(if2.quantity_grams_used * item_record.quantity) AS filament_consumed
                    FROM item_filament if2
                    WHERE if2.item_id = item_record.item_id
                ) AS consumed
                ON (consumed.filament_id = f.id)
                WHEN MATCHED THEN
                    UPDATE SET current_filament_grams = f.current_filament_grams - consumed.filament_consumed;

                RAISE NOTICE '  Updated filament quantities for item_id: %', item_record.item_id;
            ELSE
                 RAISE WARNING '  Skipping stock record due to NULL item_id (%) or quantity (%) associated with filename: %',
                     item_record.item_id, item_record.quantity, NEW.filename;
            END IF;
        END LOOP;

        -- If no items were found for the filename, log a warning
        IF NOT FOUND THEN
             RAISE WARNING 'No associated item IDs or quantities found in printer_file_models for filename: %. Cannot record stock.',
                 NEW.filename;
        END IF;

    END IF;

    RETURN NEW;
END;
$function$;

-- Add comment explaining the enhancement
COMMENT ON FUNCTION public.record_stock_on_print_finish() IS
'Trigger function that creates stock transactions when a print job finishes.
Enhanced to support item variations by matching filament colors from printer_job_filaments
(was_used=true) with color_variation_mapping. Supports multi-color prints by checking if ANY
of the used filaments match the variation mapping criteria.';

-- Apply to the benchmarks database as an administrator before deploying this version.
-- Existing rows stay NULL until their frozen-file previews are generated.
ALTER TABLE benchmarks
    ADD COLUMN IF NOT EXISTS target_preview MEDIUMTEXT NULL
    COMMENT 'Frozen desktop and mobile target-series PNG images (JSON)';

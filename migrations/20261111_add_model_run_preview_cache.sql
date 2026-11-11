-- Apply as a database administrator to the existing benchmarks database.
CREATE TABLE IF NOT EXISTS model_run_preview_cache (
    run_qid VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    artifact_commit CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    format_version SMALLINT UNSIGNED NOT NULL,
    payload MEDIUMTEXT NOT NULL CHECK (JSON_VALID(payload)),
    generated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_qid, artifact_commit, format_version),
    INDEX preview_generated_at (generated_at)
) ENGINE=InnoDB;
-- The API account needs SELECT, INSERT, UPDATE on this table. Use the actual
-- BENCHMARKS_USER and account host when applying grants; no runtime DDL is used.
-- GRANT SELECT, INSERT, UPDATE ON benchmarks.model_run_preview_cache TO 'api-user'@'%';
-- Cleanup is administrator managed (the API account does not need DELETE).

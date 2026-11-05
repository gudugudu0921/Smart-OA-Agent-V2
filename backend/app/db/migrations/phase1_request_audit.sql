-- Phase 1 / Step 5: correlate audit records with HTTP request logs.
-- Run only after phase1_account_security.sql and after taking a backup.

USE smart_oa;

SET @request_id_column_exists = (
    SELECT COUNT(*)
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'audit_logs'
      AND COLUMN_NAME = 'request_id'
);
SET @add_request_id_column = IF(
    @request_id_column_exists = 0,
    'ALTER TABLE audit_logs ADD COLUMN request_id VARCHAR(64) NULL AFTER ip_address',
    'SELECT ''audit_logs.request_id already exists'''
);
PREPARE request_id_column_statement FROM @add_request_id_column;
EXECUTE request_id_column_statement;
DEALLOCATE PREPARE request_id_column_statement;

SET @request_id_index_exists = (
    SELECT COUNT(*)
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'audit_logs'
      AND INDEX_NAME = 'idx_audit_request_id'
);
SET @add_request_id_index = IF(
    @request_id_index_exists = 0,
    'ALTER TABLE audit_logs ADD INDEX idx_audit_request_id (request_id)',
    'SELECT ''idx_audit_request_id already exists'''
);
PREPARE request_id_index_statement FROM @add_request_id_index;
EXECUTE request_id_index_statement;
DEALLOCATE PREPARE request_id_index_statement;

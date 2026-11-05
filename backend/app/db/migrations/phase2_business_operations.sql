-- Phase 2A-1: persistent actor-bound business idempotency records.
-- Back up the target database and deploy this migration before application code.
-- This migration only creates a new table and does not alter existing OA tables.
-- Execute it while connected to the configured OA schema; no schema is selected here.

CREATE TABLE IF NOT EXISTS business_operations (
    operation_id VARCHAR(36) NOT NULL COMMENT 'Backend-trusted UUID idempotency key',
    actor_user_id INT NOT NULL COMMENT 'Authenticated users.id owner',
    operation_type VARCHAR(32) NOT NULL,
    fingerprint_version SMALLINT NOT NULL DEFAULT 1,
    request_fingerprint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'PROCESSING',
    resource_type VARCHAR(32) NULL,
    resource_id VARCHAR(64) NULL,
    result_payload JSON NULL,
    error_code VARCHAR(64) NULL,
    lease_owner VARCHAR(36) NULL,
    lease_expires_at DATETIME(6) NULL COMMENT 'UTC naive DATETIME',
    attempt_count INT NOT NULL DEFAULT 0,
    thread_id VARCHAR(36) NULL COMMENT 'Correlation only, not an authority boundary',
    first_request_id VARCHAR(64) NULL,
    created_at DATETIME(6) NOT NULL COMMENT 'UTC naive DATETIME',
    updated_at DATETIME(6) NOT NULL COMMENT 'UTC naive DATETIME',
    completed_at DATETIME(6) NULL COMMENT 'UTC naive DATETIME',
    PRIMARY KEY (operation_id),
    KEY idx_business_operations_actor_created (actor_user_id, created_at),
    KEY idx_business_operations_status_lease (status, lease_expires_at),
    KEY idx_business_operations_resource (resource_type, resource_id),
    CONSTRAINT fk_business_operations_actor_user_id_users
        FOREIGN KEY (actor_user_id) REFERENCES users (id),
    CONSTRAINT chk_business_operations_type
        CHECK (operation_type IN ('SUBMIT_LEAVE', 'CANCEL_LEAVE', 'MANAGER_REVIEW', 'HR_REVIEW')),
    CONSTRAINT chk_business_operations_status
        CHECK (status IN ('PROCESSING', 'SUCCEEDED', 'FAILED', 'UNKNOWN')),
    CONSTRAINT chk_business_operations_fingerprint_version
        CHECK (fingerprint_version > 0),
    CONSTRAINT chk_business_operations_attempt_count
        CHECK (attempt_count >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

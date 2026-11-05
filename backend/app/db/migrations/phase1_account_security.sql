-- Smart-OA-Agent Phase 1 account-security migration.
-- Target: MySQL 8.x
-- Run this once before starting application code that includes the new User fields.
-- The information_schema guards make re-running the script safe.

USE smart_oa;

SET @schema_name = DATABASE();

SET @statement = IF(
    EXISTS(
        SELECT 1
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = @schema_name
          AND TABLE_NAME = 'users'
          AND COLUMN_NAME = 'must_change_password'
    ),
    'SELECT 1',
    'ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT FALSE AFTER is_active'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

SET @statement = IF(
    EXISTS(
        SELECT 1
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = @schema_name
          AND TABLE_NAME = 'users'
          AND COLUMN_NAME = 'last_login_at'
    ),
    'SELECT 1',
    'ALTER TABLE users ADD COLUMN last_login_at DATETIME(6) NULL AFTER must_change_password'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

SET @statement = IF(
    EXISTS(
        SELECT 1
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = @schema_name
          AND TABLE_NAME = 'users'
          AND COLUMN_NAME = 'password_changed_at'
    ),
    'SELECT 1',
    'ALTER TABLE users ADD COLUMN password_changed_at DATETIME(6) NULL AFTER last_login_at'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

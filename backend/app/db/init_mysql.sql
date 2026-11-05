CREATE DATABASE IF NOT EXISTS smart_oa
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE smart_oa;

CREATE TABLE IF NOT EXISTS users (
    id INT NOT NULL AUTO_INCREMENT,
    employee_no VARCHAR(32) NOT NULL COMMENT '员工编号',
    username VARCHAR(64) NOT NULL COMMENT '登录账号',
    password_hash VARCHAR(255) NOT NULL COMMENT 'bcrypt密码哈希',
    name VARCHAR(64) NOT NULL COMMENT '姓名',
    email VARCHAR(128) NULL COMMENT '企业邮箱',
    role VARCHAR(32) NOT NULL COMMENT '用户角色',
    department VARCHAR(64) NOT NULL COMMENT '所属部门',
    position VARCHAR(64) NULL COMMENT '职位',
    manager_id INT NULL COMMENT '直属主管用户ID',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
    last_login_at DATETIME(6) NULL,
    password_changed_at DATETIME(6) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_users_employee_no (employee_no),
    UNIQUE KEY uq_users_username (username),
    UNIQUE KEY uq_users_email (email),
    KEY idx_users_role (role),
    KEY idx_users_department (department),
    KEY idx_users_manager_id (manager_id),
    CONSTRAINT fk_users_manager_id_users
        FOREIGN KEY (manager_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

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

CREATE TABLE IF NOT EXISTS leave_applications (
    id VARCHAR(36) NOT NULL COMMENT 'UUID申请编号',
    employee_id INT NOT NULL COMMENT '申请人用户ID',
    leave_type VARCHAR(32) NOT NULL COMMENT '请假类型',
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    leave_days DECIMAL(5,1) NOT NULL,
    reason TEXT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    manager_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    manager_id INT NULL COMMENT '审批主管用户ID',
    manager_comment TEXT NULL,
    manager_approved_at DATETIME NULL,
    hr_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    hr_id INT NULL COMMENT 'HR审批用户ID',
    hr_comment TEXT NULL,
    hr_approved_at DATETIME NULL,
    submitted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    cancelled_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_leave_employee_id (employee_id),
    KEY idx_leave_status (status),
    KEY idx_leave_manager_status (manager_status),
    KEY idx_leave_hr_status (hr_status),
    KEY idx_leave_start_date (start_date),
    KEY idx_leave_created_at (created_at),
    CONSTRAINT fk_leave_applications_employee_id_users
        FOREIGN KEY (employee_id) REFERENCES users (id),
    CONSTRAINT fk_leave_applications_manager_id_users
        FOREIGN KEY (manager_id) REFERENCES users (id),
    CONSTRAINT fk_leave_applications_hr_id_users
        FOREIGN KEY (hr_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS notifications (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_id INT NOT NULL COMMENT '接收通知的用户ID',
    type VARCHAR(32) NOT NULL COMMENT '通知业务类型',
    title VARCHAR(128) NOT NULL,
    content TEXT NOT NULL,
    related_application_id VARCHAR(36) NULL COMMENT '关联请假申请ID',
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    read_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_notification_user_id (user_id),
    KEY idx_notification_is_read (is_read),
    KEY idx_notification_created_at (created_at),
    CONSTRAINT fk_notifications_user_id_users
        FOREIGN KEY (user_id) REFERENCES users (id),
    CONSTRAINT fk_notifications_related_application_id_leave_applications
        FOREIGN KEY (related_application_id) REFERENCES leave_applications (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_id INT NULL COMMENT '操作用户ID',
    action VARCHAR(64) NOT NULL,
    resource_type VARCHAR(64) NOT NULL,
    resource_id VARCHAR(64) NULL,
    detail JSON NULL,
    ip_address VARCHAR(45) NULL,
    request_id VARCHAR(64) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_audit_user_id (user_id),
    KEY idx_audit_action (action),
    KEY idx_audit_created_at (created_at),
    KEY idx_audit_request_id (request_id),
    KEY idx_audit_resource (resource_type, resource_id),
    CONSTRAINT fk_audit_logs_user_id_users
        FOREIGN KEY (user_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

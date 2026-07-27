-- 세인약품 홈페이지 DB 스키마 (MariaDB)
--
-- 최초 1회 (root 계정으로):
--   CREATE DATABASE IF NOT EXISTS sein_pharma DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
--   CREATE USER 'sein'@'localhost' IDENTIFIED BY '비밀번호';
--   GRANT ALL PRIVILEGES ON sein_pharma.* TO 'sein'@'localhost';
--   FLUSH PRIVILEGES;
--
-- 이후: mysql -u sein -p sein_pharma < schema.sql

CREATE TABLE IF NOT EXISTS inquiries (
    id           INT UNSIGNED NOT NULL AUTO_INCREMENT,
    name         VARCHAR(50)  NOT NULL,
    phone        VARCHAR(20)  NOT NULL,
    inquiry_type VARCHAR(30)  NULL,        -- CSO 품목 거래 / 개원컨설팅 / 병원마케팅 / 영업 파트너 지원 / 기타 문의
    message      TEXT         NULL,
    status       ENUM('신규','상담중','완료') NOT NULL DEFAULT '신규',
    admin_memo   TEXT         NULL,
    ip_address   VARCHAR(45)  NULL,
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME     NULL ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_status (status),
    KEY idx_type (inquiry_type),
    KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS admins (
    id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
    username      VARCHAR(50)  NOT NULL,
    password_hash VARCHAR(255) NOT NULL,   -- werkzeug generate_password_hash
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 관리자 계정은 CLI 로 생성:
--   flask create-admin <username> <password>

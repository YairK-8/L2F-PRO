-- L2F Warehouse Management System - Database Schema v3
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ============================================================
-- BRANCHES
-- ============================================================
CREATE TABLE IF NOT EXISTS branches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    password   TEXT NOT NULL,
    is_blocked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ============================================================
-- BARCODES CATALOG  (GLOBAL — shared across all branches)
-- ============================================================
CREATE TABLE IF NOT EXISTS barcodes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode    TEXT NOT NULL UNIQUE,
    sku        TEXT NOT NULL,
    color      TEXT NOT NULL,
    size       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_barcodes_sku     ON barcodes(sku);
CREATE INDEX IF NOT EXISTS idx_barcodes_barcode ON barcodes(barcode);

-- ============================================================
-- TAB 1 — MORNING SESSIONS
-- One row per (branch, sku, color) per day = a "work group"
-- ============================================================
CREATE TABLE IF NOT EXISTS morning_sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id    INTEGER NOT NULL REFERENCES branches(id),
    session_date TEXT NOT NULL DEFAULT (date('now','localtime')),
    sku          TEXT NOT NULL,
    color        TEXT NOT NULL,
    -- Comma-separated sizes that exist in catalog for this sku+color
    sizes_all    TEXT NOT NULL DEFAULT '',
    -- Comma-separated sizes confirmed FOUND (scanned or manually ticked)
    sizes_found  TEXT NOT NULL DEFAULT '',
    approved     INTEGER NOT NULL DEFAULT 0,  -- 0=open, 1=approved
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(branch_id, session_date, sku, color)
);
CREATE INDEX IF NOT EXISTS idx_msession_branch ON morning_sessions(branch_id, session_date);

-- ============================================================
-- TAB 1 — MISSING FLOOR  (result after approval)
-- ============================================================
CREATE TABLE IF NOT EXISTS missing_floor (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id   INTEGER NOT NULL REFERENCES branches(id),
    sku         TEXT NOT NULL,
    color       TEXT NOT NULL,
    size        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'missing',
    source      TEXT NOT NULL DEFAULT 'session',
    manual_session_date TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_missing_floor_branch ON missing_floor(branch_id, status);

-- ============================================================
-- TAB 2 — MISSING WAREHOUSE  (per branch, FIFO)
-- ============================================================
CREATE TABLE IF NOT EXISTS missing_warehouse (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id    INTEGER NOT NULL REFERENCES branches(id),
    sku          TEXT NOT NULL,
    color        TEXT NOT NULL,
    size         TEXT NOT NULL,
    quantity     INTEGER NOT NULL DEFAULT 1,
    scan_history TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    scanned_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    restocked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_missing_wh_branch  ON missing_warehouse(branch_id, status);

-- ============================================================
-- TAB 3 — WAREHOUSE LOCATIONS  (per branch)
-- ============================================================
CREATE TABLE IF NOT EXISTS warehouse_locations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id  INTEGER NOT NULL REFERENCES branches(id),
    sku        TEXT NOT NULL,
    location   TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(branch_id, sku)
);
CREATE INDEX IF NOT EXISTS idx_locations_branch ON warehouse_locations(branch_id);

-- ============================================================
-- SUPER ADMIN  (developer / master admin account)
-- Single table, typically one row.
-- ============================================================
CREATE TABLE IF NOT EXISTS admins (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT NOT NULL UNIQUE,
    password     TEXT NOT NULL,   -- bcrypt hash
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ============================================================
-- BLOCKED DEVICES (per branch)
-- ============================================================
CREATE TABLE IF NOT EXISTS blocked_devices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id    INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    device_id    TEXT NOT NULL,
    device_name  TEXT NOT NULL DEFAULT '',
    blocked_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(branch_id, device_id)
);
CREATE INDEX IF NOT EXISTS idx_blocked_devices_branch ON blocked_devices(branch_id);

-- ============================================================
-- BRANCHES — add extra fields for admin management
-- last_login and store_id added via ALTER (safe, IF NOT EXISTS via try)
-- ============================================================
-- We add these columns in db.py migrate() to avoid schema errors on re-run

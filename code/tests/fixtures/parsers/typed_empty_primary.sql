-- Ghost Data Still Exists: point-in-time database snapshot (synthetic test fixture)
-- Dialect: SQLite-compatible SQL
-- Captured at: 2026-09-03T05:00:00Z
-- Database topology: primary_only

BEGIN TRANSACTION;
CREATE TABLE customer_profiles_primary (
                resource_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                full_name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL,
                account_status TEXT NOT NULL, updated_at TEXT NOT NULL
            );
COMMIT;

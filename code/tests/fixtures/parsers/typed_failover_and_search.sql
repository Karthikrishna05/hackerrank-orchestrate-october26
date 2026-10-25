-- Ghost Data Still Exists: point-in-time database snapshot (synthetic test fixture)
-- Dialect: SQLite-compatible SQL
-- Captured at: 2026-09-02T23:00:00Z
-- Database topology: failover_replica
-- Compare the primary and any replica tables for replication drift.

BEGIN TRANSACTION;
CREATE TABLE customer_profiles_failover_replica (
                resource_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                full_name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL,
                account_status TEXT NOT NULL, updated_at TEXT NOT NULL
            );
INSERT INTO "customer_profiles_failover_replica" VALUES('account_005','user_905','Lior Haddani','LIOR.HADDANI@EXAMPLE.COM','+212 6 48 27 15 00','ACTIVE','2026-09-02T22:45:00Z');
INSERT INTO "customer_profiles_failover_replica" VALUES('account_8741','user_8741','Karim Elmar','karim.elmar@example.com','+971 50 731 0000','ACTIVE','2026-09-02T22:40:00Z');
CREATE TABLE customer_profiles_primary (
                resource_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                full_name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL,
                account_status TEXT NOT NULL, updated_at TEXT NOT NULL
            );
INSERT INTO "customer_profiles_primary" VALUES('account_8741','user_8741','Karim Elmar','karim.elmar@example.com','+971 50 731 0000','ACTIVE','2026-09-02T22:40:00Z');
CREATE TABLE search_documents_primary (resource_id TEXT PRIMARY KEY, display_name TEXT, email TEXT, source_resource_id TEXT, indexed_at TEXT);
INSERT INTO "search_documents_primary" VALUES('search_001','Jonas Bergen','jonas.bergen@example.com','account_006','2026-09-01T22:12:00Z');
COMMIT;

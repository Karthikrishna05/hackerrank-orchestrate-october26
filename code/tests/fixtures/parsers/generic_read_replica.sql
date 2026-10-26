-- Ghost Data Still Exists: point-in-time database snapshot (synthetic test fixture)
-- Dialect: SQLite-compatible SQL
-- Captured at: 2026-09-21T10:00:00Z
-- Database topology: read_replica
-- Compare the primary and any replica tables for replication drift.

BEGIN TRANSACTION;
CREATE TABLE customer_data_primary (
            resource_id TEXT PRIMARY KEY,
            origin_system TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            record_json TEXT NOT NULL CHECK (json_valid(record_json)),
            updated_at TEXT NOT NULL
        );
INSERT INTO "customer_data_primary" VALUES('analytics_1001','analytics_platform','customer_analytics_reports','{"resource_id":"analytics_1001","user_id":null,"email":null,"phone":null,"email_hash":null,"reporting_window":"last_90_days","metrics":{"sessions_30d":9}}','2026-09-21T10:00:00Z');
INSERT INTO "customer_data_primary" VALUES('analytics_1002','analytics_platform','customer_analytics_reports','{"resource_id":"analytics_1002","user_id":"user_901","email":"T.QUILL@EXAMPLE.COM","phone":null,"email_hash":null,"reporting_window":"last_90_days","metrics":{"sessions_30d":3}}','2026-09-20T08:15:00Z');
INSERT INTO "customer_data_primary" VALUES('tombstone_201','identity_service','deleted_customer_references','{"resource_id":"tombstone_201","subject_reference":"user_901","name":null,"email":null,"phone":null,"email_hash":null,"account_status":"DELETED"}','2026-09-21T10:00:00Z');
INSERT INTO "customer_data_primary" VALUES('tombstone_202','identity_service','deleted_customer_references','{"resource_id":"tombstone_202","subject_reference":"user_902","name":"Rua Tevita","email":null,"phone":null,"email_hash":null,"account_status":"DELETED"}','2026-09-21T10:00:00Z');
INSERT INTO "customer_data_primary" VALUES('data_flow_301','integration_service','connector_configurations','{"resource_id":"data_flow_301","connector_name":"customer-events-to-warehouse-1","destination":"customer_analytics_warehouse","subject_selector":"t.quill@example.com","enabled":true,"mode":"continuous_sync"}','2026-09-21T10:00:00Z');
INSERT INTO "customer_data_primary" VALUES('data_flow_302','integration_service','connector_configurations','{"resource_id":"data_flow_302","connector_name":"customer-events-to-warehouse-2","destination":"customer_analytics_warehouse","subject_selector":"r.tevita@example.com","enabled":false,"mode":"continuous_sync"}','2026-09-21T10:00:00Z');
INSERT INTO "customer_data_primary" VALUES('backup_401','backup_service','backup_catalog','{"resource_id":"backup_401","archive_path":"s3://customer-backups/weekly/segment-1.tar.zst","subject_reference":"K.ORLOV@EXAMPLE.COM","restore_allowed":true,"encryption":"AES-256"}','2026-09-21T10:00:00Z');
INSERT INTO "customer_data_primary" VALUES('backup_402','backup_service','backup_catalog','{"resource_id":"backup_402","archive_path":"s3://customer-backups/weekly/segment-2.tar.zst","subject_reference":"user_902","restore_allowed":false,"encryption":"AES-256"}','2026-09-21T10:00:00Z');
INSERT INTO "customer_data_primary" VALUES('audit_record_501','security_service','security_audit_events','{"resource_id":"audit_record_501","subject_reference":"user_901","event_name":"account_deletion_reviewed","actor_type":"automated_privacy_workflow","integrity_seal":"sha256:000000000000000000000000000000000000000000000000000000000000040d"}','2026-09-21T10:00:00Z');
INSERT INTO "customer_data_primary" VALUES('support_601','support_platform','support_conversations','{"resource_id":"support_601","ticket_id":"CS-5601","subject_reference":"user_901","channel":"email","message_text":"[REDACTED]"}','2026-09-21T10:00:00Z');
INSERT INTO "customer_data_primary" VALUES('support_602','support_platform','support_conversations','{"resource_id":"support_602","ticket_id":"CS-5602","subject_reference":"user_902","channel":"email","message_text":"Please send the delivery update to r.tevita@example.com. The affected order includes: Travel backpack."}','2026-09-21T10:00:00Z');
INSERT INTO "customer_data_primary" VALUES('search_701','search_service','customer_search_documents','{"resource_id":"search_701","index_name":"customer_directory_v3","document":{"display_name":"Tomas Quill","email":"t.quill@example.com","customer_reference":"user_901"}}','2026-09-21T10:00:00Z');
CREATE TABLE customer_data_read_replica (
            resource_id TEXT PRIMARY KEY,
            origin_system TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            record_json TEXT NOT NULL CHECK (json_valid(record_json)),
            updated_at TEXT NOT NULL
        );
INSERT INTO "customer_data_read_replica" VALUES('analytics_1001','analytics_platform','customer_analytics_reports','{"resource_id":"analytics_1001","user_id":null,"email":null,"phone":null,"email_hash":null,"reporting_window":"last_90_days","metrics":{"sessions_30d":9}}','2026-09-21T10:00:00Z');
INSERT INTO "customer_data_read_replica" VALUES('search_701','search_service','customer_search_documents','{"resource_id":"search_701","index_name":"customer_directory_v3","document":{"display_name":"Tomas Quill","email":"t.quill@example.com","customer_reference":"user_901"}}','2026-09-21T10:00:00Z');
INSERT INTO "customer_data_read_replica" VALUES('account_994','account_service','customer_profiles','{"resource_id":"account_994","customer_reference":"user_903","full_name":"Dana Kopecka","email":"D.KOPECKA@EXAMPLE.COM","phone":"+420 601 234 567","account_status":"ACTIVE","signup_channel":"referral"}','2026-09-19T06:30:00Z');
COMMIT;

"""Deletion policy table and evidence collection -> data category mapping."""

from __future__ import annotations

from collections.abc import Iterable

from .models import Rule

# A recreation path handled by the planner, not a deletion_rules category.
DATA_FLOW = "DATA_FLOW"

RULE_CATEGORIES = frozenset({
    "USER_PROFILE", "SECONDARY_IDENTIFIER", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY",
    "SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT", "ANALYTICS_PROFILE", "PRODUCT_TELEMETRY",
    "USER_EXPORT", "MARKETING_PROFILE", "EMAIL_DELIVERY_LOG", "SECURITY_AUDIT_LOG",
    "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE", "ACCOUNT_TOMBSTONE",
})

CATEGORY_BY_COLLECTION = {
    "customer_profiles": "USER_PROFILE",
    "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "customer_analytics_reports": "ANALYTICS_PROFILE",
    "customer_analytics_profiles": "ANALYTICS_PROFILE",
    "support_conversations": "SUPPORT_MESSAGE",
    "support_records": "SUPPORT_MESSAGE",
    "ticket_attachments": "SUPPORT_ATTACHMENT",
    "customer_export_jobs": "USER_EXPORT",
    "customer_exports": "USER_EXPORT",
    "connector_configurations": DATA_FLOW,
    "connector_subscriptions": DATA_FLOW,
    "backup_catalog": "BACKUP_ARCHIVE",
    "backup_restore_entries": "BACKUP_ARCHIVE",
    "security_audit_events": "SECURITY_AUDIT_LOG",
    "security_audit_records": "SECURITY_AUDIT_LOG",
    "retained_order_records": "LEGAL_HOLD_RECORD",
    "deleted_customer_references": "ACCOUNT_TOMBSTONE",
}


def category_for(collection: str | None = None, record_type: str | None = None) -> str | None:
    """Category from a known `record_type` (wins) or the collection table; None when unknown."""
    if record_type:
        rt = record_type.strip().upper()
        if rt in RULE_CATEGORIES or rt == DATA_FLOW:
            return rt
    if collection:
        return CATEGORY_BY_COLLECTION.get(collection.strip())
    return None


class PolicyTable:
    """deletion_rules.csv keyed by data_category."""

    def __init__(self, rules: Iterable[Rule]):
        self._rules: dict[str, Rule] = {}
        for r in rules:
            if r.data_category in self._rules:
                raise ValueError(f"{r.rule_id}: duplicate data_category {r.data_category}")
            self._rules[r.data_category] = r

    def rule(self, category: str | None) -> Rule | None:
        """The rule for a category, or None."""
        return self._rules.get(category) if category else None

    def action(self, category: str | None) -> str | None:
        """DELETE | REDACT | ANONYMIZE | RETAIN for a category, or None when it has no rule."""
        r = self.rule(category)
        return r.required_action if r else None

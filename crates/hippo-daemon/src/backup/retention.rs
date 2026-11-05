//! Pure retention planner: decides which verified artifacts to delete.

use super::artifact::ArtifactKind;
use super::manifest::ManifestEntry;
use chrono::{DateTime, Datelike};
use std::collections::{BTreeMap, BTreeSet};

pub const DAY_MS: i64 = 86_400_000;
pub const DELTA_KEEP_DAYS: i64 = 90;
pub const FULL_KEEP_DAYS: i64 = 365;
pub const CHECKPOINT_KEEP_DAYS: i64 = 365;

/// Returns the sorted ids to delete. Only verified entries are considered; unverified
/// entries are never deleted and never protect anything.
pub fn plan_prune(entries: &[ManifestEntry], now_ms: i64) -> Vec<String> {
    let verified: Vec<&ManifestEntry> = entries.iter().filter(|e| e.verified).collect();
    let within = |e: &ManifestEntry, days: i64| now_ms - e.meta.created_ms <= days * DAY_MS;

    // Earliest verified full of each UTC (year, month). An unrepresentable timestamp has no
    // month, so such an entry is kept rather than guessed at.
    let month = |e: &ManifestEntry| {
        DateTime::from_timestamp_millis(e.meta.created_ms).map(|t| (t.year(), t.month()))
    };
    let mut first_of_month: BTreeMap<(i32, u32), &ManifestEntry> = BTreeMap::new();
    for e in verified
        .iter()
        .filter(|e| e.meta.kind == ArtifactKind::Full)
    {
        if let Some(m) = month(e) {
            let slot = first_of_month.entry(m).or_insert(e);
            if e.meta.created_ms < slot.meta.created_ms {
                *slot = e;
            }
        }
    }
    let newest_uploaded_full = verified
        .iter()
        .filter(|e| e.meta.kind == ArtifactKind::Full && e.uploaded)
        .max_by_key(|e| e.meta.created_ms)
        .map(|e| e.meta.id.as_str());

    let kept_delta_bases: BTreeSet<&str> = verified
        .iter()
        .filter(|e| e.meta.kind == ArtifactKind::Delta && within(e, DELTA_KEEP_DAYS))
        .filter_map(|e| e.meta.base_id.as_deref())
        .collect();

    let keep = |e: &ManifestEntry| match e.meta.kind {
        ArtifactKind::Delta => within(e, DELTA_KEEP_DAYS),
        ArtifactKind::Checkpoint => within(e, CHECKPOINT_KEEP_DAYS),
        ArtifactKind::Full => {
            within(e, FULL_KEEP_DAYS)
                || month(e)
                    .is_none_or(|m| first_of_month.get(&m).is_some_and(|f| std::ptr::eq(*f, e)))
                || newest_uploaded_full == Some(e.meta.id.as_str())
                || kept_delta_bases.contains(e.meta.id.as_str())
        }
    };

    let mut doomed: Vec<String> = verified
        .iter()
        .filter(|e| !keep(e))
        .map(|e| e.meta.id.clone())
        .collect();
    doomed.sort();
    doomed
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backup::artifact::ArtifactMeta;

    const NOW: i64 = 1_800_000_000_000; // 2027-01-15 UTC

    fn e(id: &str, kind: ArtifactKind, age_days: i64, base: Option<&str>) -> ManifestEntry {
        ManifestEntry {
            meta: ArtifactMeta {
                id: id.into(),
                kind,
                label: None,
                created_ms: NOW - age_days * DAY_MS,
                base_id: base.map(Into::into),
                hippo_version: "0".into(),
                schema_version: 29,
                db_sha256: "x".into(),
                row_counts: BTreeMap::new(),
            },
            file: format!("{id}.age"),
            ciphertext_sha256: "y".into(),
            verified: true,
            shipped_ms: Some(1),
            uploaded: true,
        }
    }

    fn ids(v: Vec<String>) -> Vec<&'static str> {
        v.into_iter().map(|s| &*s.leak()).collect()
    }

    // Two fulls in the same UTC month so only the earlier is "first of month".
    // NOW-400d and NOW-399d fall in the same month (Dec 2025).
    fn old_pair() -> Vec<ManifestEntry> {
        vec![
            e("first", ArtifactKind::Full, 400, None),
            e("second", ArtifactKind::Full, 399, None),
        ]
    }

    #[test]
    fn prune_delta_after_90_days() {
        let es = vec![
            e("d", ArtifactKind::Delta, 91, Some("f")),
            e("f", ArtifactKind::Full, 1, None),
        ];
        assert_eq!(ids(plan_prune(&es, NOW)), vec!["d"]);
    }

    #[test]
    fn keep_delta_at_89_days() {
        let es = vec![
            e("d", ArtifactKind::Delta, 89, Some("f")),
            e("f", ArtifactKind::Full, 1, None),
        ];
        assert!(plan_prune(&es, NOW).is_empty());
        let es = vec![
            e("d", ArtifactKind::Delta, 90, Some("f")),
            e("f", ArtifactKind::Full, 1, None),
        ];
        assert!(plan_prune(&es, NOW).is_empty());
    }

    #[test]
    fn prune_weekly_full_after_365_days_unless_first_of_month() {
        let mut es = old_pair();
        es.push(e("new", ArtifactKind::Full, 1, None));
        assert_eq!(ids(plan_prune(&es, NOW)), vec!["second"]);
    }

    #[test]
    fn keep_first_full_of_utc_month_forever() {
        let es = vec![
            e("old", ArtifactKind::Full, 3 * 365, None),
            e("new", ArtifactKind::Full, 1, None),
        ];
        assert!(plan_prune(&es, NOW).is_empty());
    }

    #[test]
    fn checkpoint_never_counts_as_first_of_month() {
        let es = vec![
            e("c", ArtifactKind::Checkpoint, 400, None),
            e("f", ArtifactKind::Full, 399, None),
            e("new", ArtifactKind::Full, 1, None),
        ];
        assert_eq!(ids(plan_prune(&es, NOW)), vec!["c"]);
    }

    #[test]
    fn prune_checkpoint_after_365_days() {
        let es = vec![
            e("c", ArtifactKind::Checkpoint, 366, None),
            e("c2", ArtifactKind::Checkpoint, 365, None),
            e("f", ArtifactKind::Full, 1, None),
        ];
        assert_eq!(ids(plan_prune(&es, NOW)), vec!["c"]);
    }

    #[test]
    fn keep_full_referenced_by_retained_delta() {
        let mut es = old_pair();
        es.push(e("d", ArtifactKind::Delta, 10, Some("second")));
        es.push(e("new", ArtifactKind::Full, 1, None));
        assert!(plan_prune(&es, NOW).is_empty());
    }

    #[test]
    fn full_referenced_only_by_pruned_delta_is_pruned() {
        let mut es = old_pair();
        es.push(e("d", ArtifactKind::Delta, 100, Some("second")));
        es.push(e("new", ArtifactKind::Full, 1, None));
        assert_eq!(ids(plan_prune(&es, NOW)), vec!["d", "second"]);
    }

    #[test]
    fn keep_newest_verified_uploaded_full() {
        // Both fulls are >365d old and in the same month; "second" is not first-of-month.
        let es = old_pair();
        assert!(plan_prune(&es, NOW).is_empty());
    }

    #[test]
    fn newest_full_rule_ignores_not_uploaded() {
        let mut es = old_pair();
        es[1].uploaded = false; // "second" is newest but not uploaded
        es.push(e("third", ArtifactKind::Full, 398, None)); // same month, newest uploaded
        // first: first-of-month. second: pruned (not uploaded). third: newest uploaded.
        assert_eq!(ids(plan_prune(&es, NOW)), vec!["second"]);
    }

    #[test]
    fn unverified_never_deleted_and_never_protects() {
        let mut es = old_pair();
        es.push(e("d", ArtifactKind::Delta, 10, Some("second")));
        es[2].verified = false; // unverified delta does not protect "second"
        es.push(e("old_unv", ArtifactKind::Delta, 200, None));
        es[3].verified = false;
        es.push(e("new", ArtifactKind::Full, 1, None));
        assert_eq!(ids(plan_prune(&es, NOW)), vec!["second"]);
    }

    #[test]
    fn unverified_full_is_not_first_of_month() {
        let mut es = old_pair();
        es[0].verified = false;
        es.push(e("new", ArtifactKind::Full, 1, None));
        // "second" is now the earliest verified full of its month.
        assert!(plan_prune(&es, NOW).is_empty());
    }

    #[test]
    fn output_is_sorted() {
        let es = vec![
            e("z", ArtifactKind::Delta, 200, None),
            e("a", ArtifactKind::Delta, 300, None),
            e("f", ArtifactKind::Full, 1, None),
        ];
        assert_eq!(ids(plan_prune(&es, NOW)), vec!["a", "z"]);
    }
}

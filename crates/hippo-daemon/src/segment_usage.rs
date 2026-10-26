//! Model, effort, and token accounting shared by the Claude Code and Codex
//! segment extractors. These values are metadata: they never feed
//! `content_hash`, so a change to them never re-enqueues enrichment.

use std::collections::HashSet;

/// Token counts summed over one segment.
///
/// `input_tokens` excludes cached tokens. Cached reads and cache writes have
/// their own fields, so the four values never overlap.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct UsageTotals {
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cache_read_tokens: i64,
    pub cache_creation_tokens: i64,
}

impl UsageTotals {
    fn add(&mut self, other: UsageTotals) {
        self.input_tokens += other.input_tokens;
        self.output_tokens += other.output_tokens;
        self.cache_read_tokens += other.cache_read_tokens;
        self.cache_creation_tokens += other.cache_creation_tokens;
    }
}

/// Counts string values and reports the most frequent one. Ties go to the
/// value seen first.
#[derive(Debug, Clone, Default)]
struct Tally(Vec<(String, usize)>);

impl Tally {
    fn record(&mut self, value: &str) {
        match self.0.iter_mut().find(|(v, _)| v == value) {
            Some((_, n)) => *n += 1,
            None => self.0.push((value.to_string(), 1)),
        }
    }

    fn winner(&self) -> Option<&str> {
        self.0
            .iter()
            .fold(None, |best: Option<&(String, usize)>, cur| match best {
                Some(b) if b.1 >= cur.1 => Some(b),
                _ => Some(cur),
            })
            .map(|(v, _)| v.as_str())
    }
}

/// Model, effort, and token usage observed within one segment.
#[derive(Debug, Clone, Default)]
pub struct SegmentUsage {
    totals: UsageTotals,
    seen_message_ids: HashSet<String>,
    models: Tally,
    requested_models: Tally,
    efforts: Tally,
}

impl SegmentUsage {
    pub fn totals(&self) -> UsageTotals {
        self.totals
    }

    /// `input_tokens + output_tokens`.
    pub fn token_count(&self) -> i64 {
        self.totals.input_tokens + self.totals.output_tokens
    }

    pub fn model(&self) -> Option<&str> {
        self.models.winner()
    }

    pub fn requested_model(&self) -> Option<&str> {
        self.requested_models.winner()
    }

    pub fn effort(&self) -> Option<&str> {
        self.efforts.winner()
    }

    pub fn observe_model(&mut self, value: &str) {
        self.models.record(value);
    }

    pub fn observe_requested_model(&mut self, value: &str) {
        self.requested_models.record(value);
    }

    pub fn observe_effort(&mut self, value: &str) {
        self.efforts.record(value);
    }

    /// Add usage once per `message_id`. A repeat of an id already counted in
    /// this segment is ignored. Usage without an id cannot be deduplicated and
    /// is always added.
    pub fn add_once(&mut self, message_id: Option<&str>, usage: UsageTotals) {
        if let Some(id) = message_id
            && !self.seen_message_ids.insert(id.to_string())
        {
            return;
        }
        self.totals.add(usage);
    }

    /// Add usage unconditionally (Codex counter deltas are already deduplicated).
    pub fn add(&mut self, usage: UsageTotals) {
        self.totals.add(usage);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn winner_is_most_frequent_with_first_seen_tie_break() {
        let mut t = Tally::default();
        assert_eq!(t.winner(), None);
        for v in ["a", "b", "b", "a", "c"] {
            t.record(v);
        }
        assert_eq!(t.winner(), Some("a"), "a and b tie at 2; a was seen first");
        t.record("b");
        assert_eq!(t.winner(), Some("b"));
    }

    #[test]
    fn add_once_ignores_repeated_message_id() {
        let mut u = SegmentUsage::default();
        let usage = UsageTotals {
            input_tokens: 10,
            output_tokens: 5,
            cache_read_tokens: 3,
            cache_creation_tokens: 2,
        };
        u.add_once(Some("msg_1"), usage);
        u.add_once(Some("msg_1"), usage);
        u.add_once(None, usage);
        u.add_once(None, usage);
        assert_eq!(u.totals().input_tokens, 30);
        assert_eq!(u.token_count(), 45);
    }
}

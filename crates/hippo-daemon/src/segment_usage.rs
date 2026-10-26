//! Model, effort, and token accounting shared by the Claude Code and Codex
//! segment extractors. These values are metadata: they never feed
//! `content_hash`, so a change to them never re-enqueues enrichment.

use std::collections::HashMap;

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

    fn field_max(&mut self, other: UsageTotals) {
        self.input_tokens = self.input_tokens.max(other.input_tokens);
        self.output_tokens = self.output_tokens.max(other.output_tokens);
        self.cache_read_tokens = self.cache_read_tokens.max(other.cache_read_tokens);
        self.cache_creation_tokens = self.cache_creation_tokens.max(other.cache_creation_tokens);
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
    /// Usage added without a message id (Codex growth, or a record with no id).
    unkeyed: UsageTotals,
    /// Usage per `message.id`, field-wise maximum over the records seen.
    by_message_id: HashMap<String, UsageTotals>,
    models: Tally,
    requested_models: Tally,
    efforts: Tally,
}

impl SegmentUsage {
    pub fn totals(&self) -> UsageTotals {
        self.by_message_id
            .values()
            .fold(self.unkeyed, |mut acc, u| {
                acc.add(*u);
                acc
            })
    }

    /// `input_tokens + output_tokens`.
    pub fn token_count(&self) -> i64 {
        let t = self.totals();
        t.input_tokens + t.output_tokens
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

    /// `<synthetic>` marks client-generated messages that no model served. It
    /// is not a model and never counts.
    pub fn observe_model(&mut self, value: &str) {
        if value != "<synthetic>" {
            self.models.record(value);
        }
    }

    pub fn observe_requested_model(&mut self, value: &str) {
        self.requested_models.record(value);
    }

    pub fn observe_effort(&mut self, value: &str) {
        self.efforts.record(value);
    }

    /// Record usage for one API turn. Claude Code streams a turn as several
    /// records sharing `message.id`; `output_tokens` grows across them while
    /// the other fields repeat. The turn counts once, at the field-wise
    /// maximum of its records. Usage without an id cannot be deduplicated and
    /// is always added.
    pub fn record_message(&mut self, message_id: Option<&str>, usage: UsageTotals) {
        match message_id {
            Some(id) => self
                .by_message_id
                .entry(id.to_string())
                .or_default()
                .field_max(usage),
            None => self.unkeyed.add(usage),
        }
    }

    /// Add usage unconditionally (Codex counter deltas are already deduplicated).
    pub fn add(&mut self, usage: UsageTotals) {
        self.unkeyed.add(usage);
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
    fn record_message_takes_field_max_per_id() {
        let mut u = SegmentUsage::default();
        let at = |output| UsageTotals {
            input_tokens: 10,
            output_tokens: output,
            cache_read_tokens: 3,
            cache_creation_tokens: 2,
        };
        u.record_message(Some("msg_1"), at(5));
        u.record_message(Some("msg_1"), at(9));
        u.record_message(Some("msg_1"), at(7));
        u.record_message(None, at(1));
        u.record_message(None, at(1));
        let t = u.totals();
        assert_eq!((t.input_tokens, t.output_tokens), (10 + 10 + 10, 9 + 1 + 1));
        assert_eq!(u.token_count(), 30 + 11);
    }

    #[test]
    fn synthetic_model_never_wins() {
        let mut u = SegmentUsage::default();
        for _ in 0..3 {
            u.observe_model("<synthetic>");
        }
        assert_eq!(u.model(), None);
        u.observe_model("claude-opus-5");
        assert_eq!(u.model(), Some("claude-opus-5"));
    }
}

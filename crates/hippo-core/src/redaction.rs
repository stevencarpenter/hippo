use regex::{Regex, RegexSet};
use std::path::Path;

use crate::config::RedactConfig;

pub struct RedactionEngine {
    regex_set: RegexSet,
    patterns: Vec<(Regex, String)>,
    names: Vec<String>,
}

pub struct RedactionResult {
    pub text: String,
    pub count: u32,
    /// Per-rule hit breakdown: `(rule_name, hits)`. Vec (not HashMap) because
    /// the rule set is small (O(10)) and we want deterministic iteration order
    /// for metric emission. Empty when no rules fired.
    pub hits: Vec<(String, u32)>,
}

impl RedactionEngine {
    pub fn new(config: &RedactConfig) -> Result<Self, regex::Error> {
        let raw_patterns: Vec<&str> = config.patterns.iter().map(|p| p.regex.as_str()).collect();
        let regex_set = RegexSet::new(&raw_patterns)?;
        let patterns = config
            .patterns
            .iter()
            .map(|p| Ok((Regex::new(&p.regex)?, p.replacement.clone())))
            .collect::<Result<Vec<_>, regex::Error>>()?;
        let names = config.patterns.iter().map(|p| p.name.clone()).collect();
        Ok(Self {
            regex_set,
            patterns,
            names,
        })
    }

    pub fn from_config_path(path: &Path) -> anyhow::Result<Self> {
        let content = std::fs::read_to_string(path)?;
        let config: RedactConfig = toml::from_str(&content)?;
        Ok(Self::new(&config)?)
    }

    pub fn builtin() -> Self {
        Self::new(&RedactConfig::builtin()).expect("builtin patterns must compile")
    }

    pub fn redact(&self, input: &str) -> RedactionResult {
        let mut text = input.to_string();
        let mut total = 0u32;
        let mut hits: Vec<(String, u32)> = Vec::new();
        for idx in self.regex_set.matches(input).into_iter() {
            let (regex, replacement) = &self.patterns[idx];
            // Count occurrences in the pre-replacement text. Counting after
            // `replace_all` would typically return 0 (since the replacement
            // marker — e.g. `[REDACTED]` — does not itself match the pattern),
            // which would drop the hit attribution for this rule.
            let n = regex.find_iter(&text).count() as u32;
            if n == 0 {
                // RegexSet said this pattern matched, but after earlier
                // replacements the hit is gone. Nothing to count or replace.
                continue;
            }
            text = regex.replace_all(&text, replacement.as_str()).to_string();
            total += n;
            hits.push((self.names[idx].clone(), n));
        }
        RedactionResult {
            text,
            count: total,
            hits,
        }
    }

    pub fn test_string(&self, input: &str) -> Vec<String> {
        self.regex_set
            .matches(input)
            .into_iter()
            .map(|idx| self.names[idx].clone())
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn engine() -> RedactionEngine {
        RedactionEngine::builtin()
    }

    #[test]
    fn test_load_engine_from_config_path() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("redact.toml");
        std::fs::write(
            &path,
            r#"
[[patterns]]
name = "custom"
regex = "secret_\\w+"
replacement = "***"
"#,
        )
        .unwrap();

        let engine = RedactionEngine::from_config_path(&path).unwrap();
        let result = engine.redact("echo secret_token");
        assert_eq!(result.text, "echo ***");
        assert_eq!(result.count, 1);
    }

    #[test]
    fn test_no_redaction_needed() {
        let result = engine().redact("ls -la /tmp");
        assert_eq!(result.text, "ls -la /tmp");
        assert_eq!(result.count, 0);
    }

    #[test]
    fn test_redact_aws_key() {
        let result = engine().redact("export AWS_KEY=AKIAIOSFODNN7EXAMPLE");
        assert!(result.text.contains("[REDACTED]"));
        assert!(!result.text.contains("AKIAIOSFODNN7EXAMPLE"));
        assert!(result.count >= 1);
    }

    #[test]
    fn test_redact_github_pat() {
        let pat = format!("ghp_{}", "a".repeat(36));
        let input = format!("git clone https://{}@github.com/repo", pat);
        let result = engine().redact(&input);
        assert!(result.text.contains("[REDACTED]"));
        assert!(!result.text.contains(&pat));
    }

    #[test]
    fn test_redact_jwt() {
        let jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U";
        let input = format!("curl -H 'Authorization: Bearer {}'", jwt);
        let result = engine().redact(&input);
        assert!(!result.text.contains("eyJhbGciOiJIUzI1NiJ9"));
        assert!(result.count >= 1);
    }

    #[test]
    fn test_redact_generic_secret() {
        let result = engine().redact("export API_KEY=sk-1234567890abcdef");
        assert!(result.text.contains("[REDACTED]"));
        assert!(!result.text.contains("sk-1234567890abcdef"));
    }

    #[test]
    fn test_redact_bearer_header() {
        let result = engine().redact("Authorization: Bearer mytoken123456");
        assert!(result.text.contains("[REDACTED]"));
        assert!(!result.text.contains("mytoken123456"));
    }

    #[test]
    fn test_redact_private_key() {
        let result = engine().redact("-----BEGIN RSA PRIVATE KEY-----");
        assert!(result.text.contains("[REDACTED]"));
        assert!(!result.text.contains("BEGIN RSA PRIVATE KEY"));
    }

    #[test]
    fn test_no_false_positive_on_cache_key() {
        let result = engine().redact("CACHE_KEY=foo");
        assert_eq!(result.text, "CACHE_KEY=foo");
        assert_eq!(result.count, 0);
    }

    #[test]
    fn test_test_string_returns_pattern_names() {
        let names = engine().test_string("AKIAIOSFODNN7EXAMPLE");
        assert!(names.contains(&"aws_access_key".to_string()));
    }

    #[test]
    fn test_multiple_redactions() {
        let input = "AWS=AKIAIOSFODNN7EXAMPLE and API_KEY=supersecretvalue123";
        let result = engine().redact(input);
        assert!(!result.text.contains("AKIAIOSFODNN7EXAMPLE"));
        assert!(!result.text.contains("supersecretvalue123"));
        assert!(result.count >= 2);
    }

    // -----------------------------------------------------------------------
    // Per-rule hit attribution tests (issue #52).
    // -----------------------------------------------------------------------

    #[test]
    fn hits_empty_when_no_redaction() {
        let result = engine().redact("ls -la /tmp");
        assert!(result.hits.is_empty());
        assert_eq!(result.count, 0);
    }

    #[test]
    fn hits_names_the_firing_rule() {
        let result = engine().redact("AWS_KEY=AKIAIOSFODNN7EXAMPLE");
        assert!(!result.hits.is_empty(), "expected at least one hit");
        let names: Vec<&str> = result.hits.iter().map(|(n, _)| n.as_str()).collect();
        assert!(
            names.contains(&"aws_access_key"),
            "expected aws_access_key in hits, got {names:?}"
        );
    }

    #[test]
    fn hits_total_matches_count() {
        let input = "AWS=AKIAIOSFODNN7EXAMPLE and API_KEY=supersecretvalue123";
        let result = engine().redact(input);
        let hits_sum: u32 = result.hits.iter().map(|(_, n)| *n).sum();
        assert_eq!(
            hits_sum, result.count,
            "sum of per-rule hits must equal aggregate count"
        );
    }

    #[test]
    fn hits_carry_per_rule_count() {
        // Same rule firing twice must aggregate into one entry with count=2,
        // not two entries — simpler for metric emission.
        let input = "AWS_A=AKIAIOSFODNN7EXAMPLE AWS_B=AKIAIOSFODNN7ANOTHER";
        let result = engine().redact(input);
        let aws_hits: Vec<&(String, u32)> = result
            .hits
            .iter()
            .filter(|(n, _)| n == "aws_access_key")
            .collect();
        assert_eq!(aws_hits.len(), 1, "expected single entry per rule");
        assert!(
            aws_hits[0].1 >= 2,
            "expected hit count ≥ 2 for duplicate pattern, got {}",
            aws_hits[0].1
        );
    }

    // -----------------------------------------------------------------------
    // Negative cases for capture-reliability F-4 (issue #52).
    //
    // Goal: strings that LOOK secret-adjacent but are not secrets must pass
    // through unredacted. Over-redaction is a silent data-loss bug — the
    // enrichment pipeline cannot recover information from `[REDACTED]`, and
    // the RAG layer returns degraded answers.
    //
    // Test matrix: docs/capture-reliability/09-test-matrix.md row F-4
    // Invariant:   I-5 Redaction correctness
    //
    // One test per pattern class so a regression pinpoints exactly which
    // real-world shape the redaction engine mis-classified.
    // -----------------------------------------------------------------------

    fn assert_not_redacted(input: &str) {
        let result = engine().redact(input);
        assert_eq!(
            result.text, input,
            "false-positive redaction on {input:?}: became {:?}",
            result.text
        );
        assert_eq!(
            result.count,
            0,
            "false-positive count > 0 on {input:?}: names={:?}",
            engine().test_string(input)
        );
    }

    #[test]
    fn redact_preserves_uuid_v4() {
        // Canonical UUID4 — appears in Claude session IDs, transcript
        // paths, envelope IDs. A false-positive here would obliterate
        // every session row.
        assert_not_redacted("session_id=550e8400-e29b-41d4-a716-446655440000");
    }

    #[test]
    fn redact_preserves_git_short_sha() {
        // 7-char commit SHA — below the 8-char generic_secret_assignment
        // threshold but close. "commit=abc1234" must stay readable so
        // enrichment can link events to commits.
        assert_not_redacted("commit=abc1234");
    }

    #[test]
    fn redact_preserves_git_full_sha() {
        // 40-char git SHA looks like a token but must not match any rule.
        assert_not_redacted("HEAD is at 5f3a9c2e1b8d7f6a4c3e2d1b9a8f7e6d5c4b3a2e");
    }

    #[test]
    fn redact_preserves_cargo_lockfile_checksum() {
        // Representative Cargo.lock line — base16 digest, 64 chars.
        // Users will run `cargo build` and see this in shell output.
        assert_not_redacted(
            "checksum = \"3a4b5c6d7e8f9012a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f80912\"",
        );
    }

    #[test]
    fn redact_preserves_jwt_lookalike_base64() {
        // Random base64 in log output that does NOT have the JWT three-
        // part shape. The `jwt` pattern must only fire on actual JWTs.
        assert_not_redacted("data: dGhpc2lzbm90YWp3dGp1c3RhYmFzZTY0c3RyaW5n");
    }

    #[test]
    fn redact_preserves_partial_aws_prefix() {
        // "AKIA" prefix alone must not trigger — the rule requires 16
        // subsequent [0-9A-Z]. "AKIA" followed by lowercase is a
        // plausible false-positive shape.
        assert_not_redacted("variable_name = AKIAlowercase_suffix");
    }

    #[test]
    fn redact_preserves_ghp_short_prefix() {
        // Only the literal prefix without 36 more chars. Users discussing
        // "the ghp_ pattern" must not have their text redacted.
        assert_not_redacted("the token prefix is ghp_ for personal access tokens");
    }

    #[test]
    fn redact_preserves_harmless_api_mention() {
        // "api key" mentioned in prose without an assignment that meets
        // the 8-char minimum. The `\s*[=:]\s*\S{8,}` tail must enforce
        // a separator + length; prose should pass through.
        assert_not_redacted("we need to set the api_key before running");
    }

    #[test]
    fn redact_preserves_bearer_token_word_in_prose() {
        // "bearer" without the `authorization:` prefix must not trigger.
        assert_not_redacted("this function returns a bearer record from the state");
    }

    #[test]
    fn redact_preserves_private_key_path_reference() {
        // A path mentioning a private key file is NOT key material.
        assert_not_redacted("ssh -i ~/.ssh/id_ed25519 user@host");
    }

    #[test]
    fn redact_preserves_public_key_pem_header() {
        // `-----BEGIN PUBLIC KEY-----` must NOT be redacted — the pattern
        // is deliberately scoped to `PRIVATE KEY`. This guards against a
        // future over-broadening of the regex.
        assert_not_redacted("-----BEGIN PUBLIC KEY-----");
    }

    #[test]
    fn redact_preserves_hexadecimal_hash_in_url() {
        // Long hex string as part of a URL path — common in artifact
        // links, GitHub blob URLs, content-addressed storage.
        assert_not_redacted(
            "https://example.com/blob/1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9012/file.txt",
        );
    }

    #[test]
    fn redact_preserves_docker_image_digest() {
        // `sha256:...` digest syntax — all valid in normal shell output.
        assert_not_redacted(
            "docker pull nginx@sha256:abc123def456789012345678901234567890abcdef123456789012345678901234",
        );
    }

    #[test]
    fn redact_preserves_auth_header_prose() {
        // The word "authorization" in prose (no bearer token, no
        // key=value pair with 8+ chars). The `bearer_header` rule is
        // bearer-specific; this asserts it stays that way.
        assert_not_redacted("the Authorization: header carries the credential");
    }

    #[test]
    fn redact_count_stays_zero_on_plain_git_output() {
        // Representative `git log --oneline` output — the top failure
        // shape in #52. Many redaction regexes, zero secrets.
        let sample = "abc1234 fix(install): configure Claude session hook\n\
             def5678 feat: add source_health table\n\
             0a1b2c3 docs: update capture reliability overview\n";
        let result = engine().redact(sample);
        assert_eq!(
            result.count,
            0,
            "false positives on git output: names={:?}",
            engine().test_string(sample)
        );
        assert_eq!(result.text, sample);
    }
}

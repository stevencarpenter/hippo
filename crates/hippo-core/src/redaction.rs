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
        self.regex_set.matches(input).into_iter().fold(
            RedactionResult {
                text: input.to_string(),
                count: 0,
            },
            |acc, idx| {
                let (regex, replacement) = &self.patterns[idx];
                let hits = regex.find_iter(&acc.text).count() as u32;
                RedactionResult {
                    text: regex
                        .replace_all(&acc.text, replacement.as_str())
                        .to_string(),
                    count: acc.count + hits,
                }
            },
        )
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
}

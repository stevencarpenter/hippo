//! Parser for GitHub Actions annotations → (tool, rule_id) tuples.

use regex::Regex;
use std::sync::LazyLock;

// Covers all ruff prefixes: E, W, F, B, N, I, S, ANN, UP, SIM, PTH, RET, RUF, etc.
static RUFF_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\b([A-Z]{1,4}\d{3,4})\b").unwrap());
static CARGO_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"error\[(E\d{4})\]").unwrap());
// Match both mypy lowercase ([arg-type]) and pyright PascalCase
// ([reportAttributeAccessIssue]). {7,} on the inner group (min 8 total chars)
// filters common noise tokens like [stderr], [error], [warning], [info].
// Note: {2,} as in the original plan does NOT suppress these — verified empirically.
static MYPY_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\[([a-zA-Z][a-zA-Z0-9-]{7,})\]\s*$").unwrap());

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedAnnotation {
    pub tool: Option<String>,
    pub rule_id: Option<String>,
}

pub fn parse(job_name: &str, message: &str) -> ParsedAnnotation {
    // Priority: Cargo (message-unambiguous) → Ruff → mypy/pyright → pytest → unknown.
    // Do not reorder without auditing overlap between regex patterns.

    // 1. Cargo is unambiguous from the message alone.
    if let Some(c) = CARGO_RE.captures(message) {
        return ParsedAnnotation {
            tool: Some("cargo".into()),
            rule_id: Some(c[1].to_string()),
        };
    }

    // 2. Ruff rule-codes: job name is a strong prior.
    let job_lower = job_name.to_ascii_lowercase();
    if (job_lower.contains("ruff") || job_lower.contains("lint"))
        && let Some(c) = RUFF_RE.captures(message)
    {
        return ParsedAnnotation {
            tool: Some("ruff".into()),
            rule_id: Some(c[1].to_string()),
        };
    }

    // 3. mypy / pyright: bracket-suffixed error code.
    if (job_lower.contains("type") || job_lower.contains("mypy") || job_lower.contains("pyright"))
        && let Some(c) = MYPY_RE.captures(message)
    {
        let tool = if job_lower.contains("pyright") {
            "pyright"
        } else {
            "mypy"
        };
        return ParsedAnnotation {
            tool: Some(tool.into()),
            rule_id: Some(c[1].to_string()),
        };
    }

    // 4. pytest: FAILED marker or AssertionError heuristic.
    if (job_lower.contains("pytest") || job_lower.contains("test"))
        && (message.contains("FAILED ") || message.contains("AssertionError"))
    {
        return ParsedAnnotation {
            tool: Some("pytest".into()),
            rule_id: None,
        };
    }

    ParsedAnnotation {
        tool: None,
        rule_id: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn call(job: &str, msg: &str) -> ParsedAnnotation {
        parse(job, msg)
    }

    #[test]
    fn ruff_rule_from_message() {
        let got = call("ruff", "F401 [*] 'os' imported but unused");
        assert_eq!(got.tool.as_deref(), Some("ruff"));
        assert_eq!(got.rule_id.as_deref(), Some("F401"));
    }

    #[test]
    fn ruff_rule_e_class() {
        let got = call("lint", "E501 line too long (120 > 100 characters)");
        assert_eq!(got.tool.as_deref(), Some("ruff"));
        assert_eq!(got.rule_id.as_deref(), Some("E501"));
    }

    #[test]
    fn cargo_rustc_error() {
        let got = call("build", "error[E0308]: mismatched types");
        assert_eq!(got.tool.as_deref(), Some("cargo"));
        assert_eq!(got.rule_id.as_deref(), Some("E0308"));
    }

    #[test]
    fn mypy_error_with_code() {
        let got = call(
            "typecheck",
            "error: Argument 1 has incompatible type [arg-type]",
        );
        assert_eq!(got.tool.as_deref(), Some("mypy"));
        assert_eq!(got.rule_id.as_deref(), Some("arg-type"));
    }

    #[test]
    fn pytest_assertion_no_rule() {
        let got = call("test", "FAILED tests/test_x.py::test_y - AssertionError");
        assert_eq!(got.tool.as_deref(), Some("pytest"));
        assert_eq!(got.rule_id, None);
    }

    #[test]
    fn unknown_falls_through() {
        let got = call("misc", "some random message");
        assert_eq!(got.tool, None);
        assert_eq!(got.rule_id, None);
    }

    #[test]
    fn ruff_extended_namespace_b006() {
        let got = call(
            "lint",
            "B006 Do not use mutable data structures for argument defaults",
        );
        assert_eq!(got.tool.as_deref(), Some("ruff"));
        assert_eq!(got.rule_id.as_deref(), Some("B006"));
    }

    #[test]
    fn pyright_native_code() {
        let got = call(
            "pyright",
            r#"Cannot access attribute "foo" for class "Bar" [reportAttributeAccessIssue]"#,
        );
        assert_eq!(got.tool.as_deref(), Some("pyright"));
        assert_eq!(got.rule_id.as_deref(), Some("reportAttributeAccessIssue"));
    }

    #[test]
    fn typecheck_stderr_noise_does_not_match() {
        let got = call("typecheck", "stream closed [stderr]");
        assert_eq!(got.tool, None);
        assert_eq!(got.rule_id, None);
    }

    #[test]
    fn uppercase_job_name() {
        let got = call("RUFF", "E501 line too long");
        assert_eq!(got.tool.as_deref(), Some("ruff"));
        assert_eq!(got.rule_id.as_deref(), Some("E501"));
    }

    #[test]
    fn assertion_error_without_failed_marker() {
        let got = call("test", "AssertionError: expected True");
        assert_eq!(got.tool.as_deref(), Some("pytest"));
        assert_eq!(got.rule_id, None);
    }
}

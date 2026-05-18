//! Render a structured tool-call `input` into a human-readable command string.
//!
//! Shared across all agentic harnesses so Claude's `Bash` call and Codex's
//! `exec_command` produce the same display shape for downstream greps.

use serde_json::Value;

pub fn render_command(tool_name: &str, input: &Value) -> String {
    match tool_name {
        "Bash" | "bash" => input
            .get("command")
            .and_then(Value::as_str)
            .unwrap_or("bash")
            .to_string(),
        "exec_command" => input
            .get("cmd")
            .and_then(Value::as_str)
            .unwrap_or("exec")
            .to_string(),
        "Read" | "read" => format!("read {}", file_path_field(input)),
        "Edit" | "edit" => format!("edit {}", file_path_field(input)),
        "Write" | "write" => format!("write {}", file_path_field(input)),
        "Grep" | "grep" => format!(
            "grep '{}' {}",
            str_field(input, "pattern", "*"),
            str_field(input, "path", ".")
        ),
        "Glob" | "glob" => format!("glob '{}'", str_field(input, "pattern", "*")),
        "Agent" | "agent" => format!("agent: {}", str_field(input, "description", "agent task")),
        "TaskCreate" => format!("task: {}", str_field(input, "subject", "task")),
        "TaskUpdate" => format!(
            "task-update: {} {}",
            str_field(input, "taskId", "?"),
            str_field(input, "status", "?")
        ),
        // Claude emits `{"name": "Skill", "input": {"skill": "<name>"}}`.
        "Skill" => format!("skill: {}", str_field(input, "skill", "?")),
        // opencode emits `{"tool": "skill", "input": {"name": "<name>"}}`
        // (verified via SQL against ~/.local/share/opencode/opencode.db).
        "skill" => format!("skill: {}", str_field(input, "name", "?")),
        // Claude WebFetch / WebSearch — frequent enough in real transcripts that
        // the bare tool-name fallback strips meaningful signal. Shape verified
        // against ~/.claude/projects/*.jsonl tool_use blocks.
        "WebFetch" => format!("fetch {}", str_field(input, "url", "<url>")),
        "WebSearch" => format!("search '{}'", str_field(input, "query", "")),
        other => other.to_string(),
    }
}

fn str_field<'a>(input: &'a Value, key: &str, default: &'a str) -> &'a str {
    input.get(key).and_then(Value::as_str).unwrap_or(default)
}

/// Return the file path from `input`, trying both the camelCase `filePath`
/// key (opencode) and the snake_case `file_path` key (Claude Code).  Falls
/// back to `"<unknown>"` when neither key is present.
fn file_path_field(input: &Value) -> &str {
    input
        .get("filePath")
        .and_then(Value::as_str)
        .or_else(|| input.get("file_path").and_then(Value::as_str))
        .unwrap_or("<unknown>")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn render_read_uses_snake_case_file_path() {
        let input = json!({"file_path": "/src/main.rs"});
        assert_eq!(render_command("Read", &input), "read /src/main.rs");
    }

    #[test]
    fn render_read_uses_camel_case_file_path() {
        let input = json!({"filePath": "/src/lib.rs"});
        assert_eq!(render_command("read", &input), "read /src/lib.rs");
    }

    #[test]
    fn render_edit_uses_camel_case_file_path() {
        let input = json!({"filePath": "/src/foo.rs"});
        assert_eq!(render_command("edit", &input), "edit /src/foo.rs");
    }

    #[test]
    fn render_write_uses_camel_case_file_path() {
        let input = json!({"filePath": "/out/bar.rs"});
        assert_eq!(render_command("write", &input), "write /out/bar.rs");
    }

    #[test]
    fn render_read_fallback_unknown_when_no_path_key() {
        let input = json!({"other": "val"});
        assert_eq!(render_command("read", &input), "read <unknown>");
    }
}

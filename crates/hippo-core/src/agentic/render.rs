//! Render a structured tool-call `input` into a human-readable command string.
//!
//! Shared across all agentic harnesses so Claude's `Bash` call and Codex's
//! `exec_command` produce the same display shape for downstream greps.

use serde_json::Value;

pub fn render_command(tool_name: &str, input: &Value) -> String {
    match tool_name {
        "Bash" => input
            .get("command")
            .and_then(Value::as_str)
            .unwrap_or("bash")
            .to_string(),
        "exec_command" => input
            .get("cmd")
            .and_then(Value::as_str)
            .unwrap_or("exec")
            .to_string(),
        "Read" => format!("read {}", str_field(input, "file_path", "<unknown>")),
        "Edit" => format!("edit {}", str_field(input, "file_path", "<unknown>")),
        "Write" => format!("write {}", str_field(input, "file_path", "<unknown>")),
        "Grep" => format!(
            "grep '{}' {}",
            str_field(input, "pattern", "*"),
            str_field(input, "path", ".")
        ),
        "Glob" => format!("glob '{}'", str_field(input, "pattern", "*")),
        "Agent" => format!("agent: {}", str_field(input, "description", "agent task")),
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
        other => other.to_string(),
    }
}

fn str_field<'a>(input: &'a Value, key: &str, default: &'a str) -> &'a str {
    input.get(key).and_then(Value::as_str).unwrap_or(default)
}

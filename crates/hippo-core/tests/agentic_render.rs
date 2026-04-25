use hippo_core::agentic::render::render_command;
use serde_json::json;

#[test]
fn bash_renders_command_verbatim() {
    let input = json!({"command": "cargo test -p hippo-core"});
    assert_eq!(render_command("Bash", &input), "cargo test -p hippo-core");
}

#[test]
fn read_renders_path() {
    assert_eq!(
        render_command("Read", &json!({"file_path": "/foo/bar.rs"})),
        "read /foo/bar.rs"
    );
}

#[test]
fn edit_renders_path() {
    assert_eq!(
        render_command("Edit", &json!({"file_path": "/foo/bar.rs"})),
        "edit /foo/bar.rs"
    );
}

#[test]
fn write_renders_path() {
    assert_eq!(
        render_command("Write", &json!({"file_path": "/foo/bar.rs"})),
        "write /foo/bar.rs"
    );
}

#[test]
fn grep_renders_pattern_and_path() {
    assert_eq!(
        render_command("Grep", &json!({"pattern": "TODO", "path": "src/"})),
        "grep 'TODO' src/"
    );
}

#[test]
fn glob_renders_pattern() {
    assert_eq!(
        render_command("Glob", &json!({"pattern": "**/*.rs"})),
        "glob '**/*.rs'"
    );
}

#[test]
fn agent_renders_description() {
    assert_eq!(
        render_command("Agent", &json!({"description": "find TODOs"})),
        "agent: find TODOs"
    );
}

#[test]
fn task_create_renders_subject() {
    assert_eq!(
        render_command("TaskCreate", &json!({"subject": "fix bug"})),
        "task: fix bug"
    );
}

#[test]
fn task_update_renders_id_and_status() {
    assert_eq!(
        render_command(
            "TaskUpdate",
            &json!({"taskId": "42", "status": "completed"})
        ),
        "task-update: 42 completed"
    );
}

#[test]
fn exec_command_renders_cmd() {
    // Codex shape
    assert_eq!(
        render_command(
            "exec_command",
            &json!({"cmd": "ls /tmp", "workdir": "/tmp"})
        ),
        "ls /tmp"
    );
}

#[test]
fn skill_renders_name() {
    // opencode shape: {"tool": "skill", "input": {"name": "<name>"}}
    assert_eq!(
        render_command("skill", &json!({"name": "brainstorming"})),
        "skill: brainstorming"
    );
}

#[test]
fn claude_skill_renders_skill_field() {
    // Claude shape: {"name": "Skill", "input": {"skill": "<name>"}}
    // Verified against ~/.claude/projects/*.jsonl tool_use blocks.
    assert_eq!(
        render_command("Skill", &json!({"skill": "superpowers:brainstorming"})),
        "skill: superpowers:brainstorming"
    );
}

#[test]
fn web_fetch_renders_url() {
    assert_eq!(
        render_command(
            "WebFetch",
            &json!({"url": "https://example.com/foo", "prompt": "..."})
        ),
        "fetch https://example.com/foo"
    );
}

#[test]
fn web_search_renders_query() {
    assert_eq!(
        render_command(
            "WebSearch",
            &json!({"query": "actions/upload-artifact latest version", "allowed_domains": ["github.com"]})
        ),
        "search 'actions/upload-artifact latest version'"
    );
}

#[test]
fn unknown_tool_returns_name() {
    assert_eq!(render_command("MadeUp", &json!({})), "MadeUp");
}

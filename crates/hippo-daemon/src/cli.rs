use clap::{Parser, Subcommand};
pub use hippo_daemon::commands::EnrichmentAction;

#[derive(Parser)]
#[command(name = "hippo", version = env!("HIPPO_VERSION_FULL"), about = "Local knowledge capture daemon")]
pub struct Cli {
    #[command(subcommand)]
    pub command: Commands,
}

#[derive(Subcommand)]
pub enum Commands {
    /// Daemon management
    Daemon {
        #[command(subcommand)]
        action: DaemonAction,
    },
    /// Brain server management
    Brain {
        #[command(subcommand)]
        action: BrainAction,
    },
    /// Send an event to the daemon
    SendEvent {
        #[command(subcommand)]
        source: SendEventSource,
    },
    /// Show daemon status
    Status,
    /// List sessions
    Sessions {
        /// Show only today's sessions
        #[arg(long)]
        today: bool,
        /// Show sessions since duration (e.g. "2h", "7d")
        #[arg(long)]
        since: Option<String>,
    },
    /// List events
    Events {
        /// Filter by session ID
        #[arg(long)]
        session: Option<i64>,
        /// Show events since duration (e.g. "2h", "7d")
        #[arg(long)]
        since: Option<String>,
        /// Filter by project name (substring match on cwd)
        #[arg(long)]
        project: Option<String>,
    },
    /// Destroy expired event outputs, unreferenced environment payloads, and expired agent-eval text
    ///
    /// Agent-eval text is `eval_findings.line_text` and `claim` and
    /// `eval_verdicts.note`. Findings whose effective verdict is `accepted`,
    /// their verdicts, and all escapes are pinned and keep their text.
    GcEventPayloads {
        /// Explicitly authorize this destructive sweep (ledger rows remain)
        #[arg(long, required = true)]
        confirm: bool,
    },
    /// Query the knowledge base
    Query {
        /// Search text
        text: String,
        /// Use raw keyword search instead of brain server
        #[arg(long)]
        raw: bool,
    },
    /// Ask a question and get a synthesized answer from the knowledge base
    Ask {
        /// The question to ask
        question: String,
    },
    /// List known entities
    Entities {
        /// Filter by entity type
        #[arg(long, name = "type")]
        entity_type: Option<String>,
    },
    /// Export training data as JSONL
    ExportTraining {
        /// Output directory
        #[arg(long, default_value = ".")]
        out: String,
        /// Export since duration (e.g. "30d")
        #[arg(long)]
        since: Option<String>,
    },
    /// Configuration management
    Config {
        #[command(subcommand)]
        action: ConfigAction,
    },
    /// Redaction tools
    Redact {
        #[command(subcommand)]
        action: RedactAction,
    },
    /// Ingest events from external sources
    Ingest {
        #[command(subcommand)]
        source: IngestSource,
    },
    /// Agent-evaluation tools
    Eval {
        #[command(subcommand)]
        action: EvalAction,
    },
    /// Run one pass of the GitHub Actions poller.
    GhPoll {
        /// Override watched repos; mainly for testing.
        #[arg(long)]
        repo: Option<String>,
    },
    /// List (and ack) pending CI failure notifications for a repo.
    GhPendingNotifications {
        #[arg(long)]
        repo: String,
        /// Mark retrieved notifications as acknowledged.
        #[arg(long)]
        ack: bool,
    },
    /// Run as Native Messaging host for Firefox extension
    NativeMessagingHost,
    /// Run diagnostic checks
    Doctor {
        /// Print remediation steps for each failing check
        #[arg(long)]
        explain: bool,
    },
    /// Run synthetic capture probes and record results in source_health
    Probe {
        /// Run only the named source probe (shell, claude-tool, agentic-session-claude,
        /// agentic-session-cursor, agentic-session-opencode, agentic-session-codex,
        /// agentic-session-pi, browser, claude-auto-memory).
        /// Omit to run all probes.
        #[arg(long)]
        source: Option<String>,
    },
    /// Capture-reliability watchdog (invoked by launchd every 60 s)
    Watchdog {
        #[command(subcommand)]
        action: WatchdogAction,
    },
    /// Capture-reliability alarm management
    Alarms {
        #[command(subcommand)]
        action: AlarmsAction,
    },
    /// Per-agent scorecards from agent sessions and review records
    Agents {
        #[command(subcommand)]
        action: AgentsAction,
    },
    /// Pause or resume LLM enrichment, or show its state
    Enrichment {
        #[command(subcommand)]
        action: EnrichmentAction,
    },
    /// Watch ~/.claude/projects/**/*.jsonl for new session content (FSEvents, KeepAlive service)
    ClaudeSessionWatch,
    /// Poll opencode's SQLite DB for new sessions (LaunchAgent, OneShot)
    OpencodePoll,
    /// Poll Codex CLI rollout files and ingest new sessions.
    CodexPoll,
    /// Poll Cursor Agent CLI transcript files and ingest new sessions.
    CursorPoll,
    /// Poll Pi agent session transcripts and ingest new sessions.
    PiPoll,
    /// Poll configured Claude auto-memory Markdown sources for ingest.
    AutoMemoryPoll,
    /// Watch configured Claude auto-memory sources via FSEvents (KeepAlive service).
    AutoMemoryWatch,
    /// Run the daemon in the foreground (alias for `daemon run`).
    ///
    /// BT-09: shipped so `hippo serve` no longer fails with "unrecognized
    /// subcommand" — bench's shadow_stack used to call this and silently
    /// crashed.
    Serve {
        /// Run in bench mode: skip FSEvents watcher and LaunchAgent guards.
        #[arg(long)]
        bench: bool,
    },
}

#[derive(Subcommand)]
pub enum AgentsAction {
    /// Print a scorecard table per agent: sessions, duration, tokens, cost,
    /// review findings, verdict rates, escapes, and benchmark trends.
    ///
    /// Reads `GET /agents/scorecard` from the brain server, the same
    /// aggregates as the MCP tool `get_agent_scorecard`. Every rate shows its
    /// denominator; a rate over fewer than 5 judged findings is marked low
    /// sample. Probe sessions are excluded.
    Report {
        /// Registry agent name (Claude subagent type or review agent); omit for every agent
        #[arg(long)]
        agent: Option<String>,
        /// Window as <digits><m|h|d>, e.g. "30d"; omit for all time
        #[arg(long)]
        since: Option<String>,
        /// Substring of the session cwd or project dir, or of the review repo slug or path
        #[arg(long)]
        project: Option<String>,
        /// Exact model id or requested model alias
        #[arg(long)]
        model: Option<String>,
    },
}

#[derive(Subcommand)]
pub enum WatchdogAction {
    /// Assert invariants against source_health and write capture_alarms rows.
    /// Designed to be invoked by launchd; exits 0 on success.
    Run,
}

#[derive(Subcommand)]
pub enum AlarmsAction {
    /// List alarms grouped by status. Exits 1 if any *active* alarms remain
    /// (auto-resolved-but-unacked rows do not affect exit code).
    List,
    /// Acknowledge an alarm by ID. Re-ack is a no-op (idempotent).
    Ack {
        /// Alarm ID to acknowledge
        id: i64,
        /// Optional note to attach to the acknowledgement
        #[arg(long)]
        note: Option<String>,
    },
    /// Bulk-acknowledge all auto-resolved alarms. Sets `acked_at` and
    /// `ack_note='auto-resolved'` on every row where the watchdog cleared
    /// the underlying invariant but the user hasn't ack'd yet.
    Prune,
}

#[derive(Subcommand)]
pub enum BrainAction {
    /// Stop the brain server (sends SIGTERM)
    Stop,
}

#[derive(Subcommand)]
pub enum DaemonAction {
    /// Run the daemon in the foreground.
    ///
    /// Post-review C-5: `--bench` is symmetric with `hippo serve --bench`.
    /// Earlier the flag only existed on `Serve`, but tracking-doc claims and
    /// shadow_stack invocations referred to both forms; the missing flag
    /// silently caused `daemon run --bench` to start in non-bench mode.
    Run {
        /// Run in bench mode: skip FSEvents watcher and LaunchAgent guards;
        /// refuse to start if db_path resolves outside `XDG_DATA_HOME`/`HOME`.
        #[arg(long)]
        bench: bool,
    },
    /// Start the daemon via launchd
    Start,
    /// Stop the daemon
    Stop,
    /// Restart the daemon
    Restart,
    /// Install LaunchAgents for daemon and brain
    Install {
        /// Overwrite existing plist files
        #[arg(long)]
        force: bool,
        /// Path to the brain project directory (defaults to XDG_DATA_HOME/hippo-brain or ~/.local/share/hippo-brain)
        #[arg(long)]
        brain_dir: Option<std::path::PathBuf>,
        /// Path to record for the `hippo` binary in the generated plists.
        ///
        /// Defaults to $HIPPO_BIN, then to the running executable. Set this
        /// when the binary is reached through a stable symlink that should
        /// outlive the file it currently points at (a nix profile path, for
        /// example) — the default resolves symlinks and would pin the plists
        /// to a path a later upgrade makes stale.
        #[arg(long)]
        binary_path: Option<std::path::PathBuf>,
    },
    /// Render and install the optional com.hippo.omlx LaunchAgent.
    ///
    /// Routes the plist through the same `render_plist` / `install_plist`
    /// path as the core agents so there's a single, tested rendering
    /// implementation. The shell-side `mise run install:omlx` wraps this
    /// with brew-services takedown, launchctl bootstrap, and a readiness
    /// probe — concerns that don't belong in the daemon binary.
    InstallOmlx {
        /// Overwrite an existing plist file
        #[arg(long)]
        force: bool,
    },
}

#[derive(Subcommand)]
pub enum SendEventSource {
    /// Send a shell command event
    Shell {
        /// The command that was run
        #[arg(long)]
        cmd: String,
        /// Exit code
        #[arg(long)]
        exit: i32,
        /// Working directory
        #[arg(long)]
        cwd: String,
        /// Duration in milliseconds
        #[arg(long)]
        duration_ms: u64,
        /// Git repository identifier (owner/repo). If omitted, the daemon derives it from cwd.
        #[arg(long)]
        git_repo: Option<String>,
        /// Git branch
        #[arg(long)]
        git_branch: Option<String>,
        /// Git commit hash
        #[arg(long)]
        git_commit: Option<String>,
        /// Git dirty flag
        #[arg(long)]
        git_dirty: bool,
        #[command(flatten)]
        capture: Box<hippo_daemon::commands::ShellOutputArgs>,
        /// Probe tag UUID: marks this event as a synthetic probe.
        /// Any unique UUID; excluded from all user queries.
        #[arg(long)]
        probe_tag: Option<String>,
        /// Override source_kind (e.g. "claude-tool"). Defaults to "shell".
        /// When set to "claude-tool", --tool-name is required.
        #[arg(long, requires_if("claude-tool", "tool_name"))]
        source_kind: Option<String>,
        /// Tool name for claude-tool events (e.g. "Bash"). Required when
        /// --source-kind=claude-tool.
        #[arg(long)]
        tool_name: Option<String>,
    },
    /// Register a SHA in the watchlist for CI tracking.
    Watchlist {
        #[arg(long)]
        sha: String,
        #[arg(long)]
        repo: String,
        #[arg(long, default_value = "1200")]
        ttl: u64,
    },
}

#[derive(Subcommand)]
pub enum ConfigAction {
    /// Create the default config if it does not exist
    Init,
    /// Open config in editor
    Edit,
    /// Set a config value
    Set {
        /// Config key (dot-separated, e.g. "daemon.flush_interval_ms")
        key: String,
        /// Config value
        value: String,
    },
}

#[derive(Subcommand)]
pub enum IngestSource {
    /// Import a Claude Code session JSONL file
    ClaudeSession {
        /// Path to the JSONL session file
        path: String,
        /// Wait up to N seconds for the file to appear before importing (default: 0 = no wait)
        #[arg(long, default_value_t = 0)]
        wait_for_file: u64,
    },
    /// Import a Pi agent session transcript JSONL file
    PiSession {
        /// Path to the JSONL transcript file
        path: String,
        /// Wait up to N seconds for the file to appear before importing (default: 0 = no wait)
        #[arg(long, default_value_t = 0)]
        wait_for_file: u64,
    },
    /// Import a Cursor Agent transcript JSONL file
    CursorSession {
        /// Path to the JSONL transcript file
        path: String,
        /// Wait up to N seconds for the file to appear before importing (default: 0 = no wait)
        #[arg(long, default_value_t = 0)]
        wait_for_file: u64,
    },
    /// Import agent-evaluation records (review runs, findings, verdicts, escapes, bench runs) from a v1 JSONL file
    ///
    /// Prints `written W, skipped S, rejected R` and one `line N: rejected: REASON`
    /// per rejected line on stderr. Records whose id already exists with equal
    /// fields are skipped, so re-ingesting a whole ledger is safe. Exits 1 when
    /// no line was written or skipped.
    AgentEval {
        /// Path to the JSONL file
        path: String,
    },
    /// Re-extract segments from previously-truncated session JSONL files (Bug A recovery).
    /// Also repairs subagent identity across the whole database, prints `Subagent agent coverage`,
    /// and on the first run after schema v28 re-enqueues segments whose `Agent` summaries changed.
    ClaudeSessionBackfill {
        /// Glob pattern matching session JSONL paths (e.g. ~/.claude/projects/**/*.jsonl)
        glob: String,
        /// Only process files whose mtime is newer than this date (YYYY-MM-DD, local time)
        #[arg(long)]
        since: Option<String>,
        /// Print matched files and summary without writing to the database
        #[arg(long)]
        dry_run: bool,
    },
}

#[derive(Subcommand)]
pub enum EvalAction {
    /// Write private blind-review bench cases from accepted findings and fixed escapes
    ///
    /// One `<case id>.json` per accepted finding (side `+`, with a fix commit from
    /// its accepted verdict or its latest automatic `likely-addressed` verdict) and
    /// per escape with a `fix_commit`. `base` is the fixed file, `head` the defective
    /// file (the review's `head_sha`, or the parent of `fix_commit`), and the trap is
    /// the flagged line as an added row. File text, claims, and notes pass through the
    /// ingest redaction engines. Case ids are `f-<finding id>` and `e-<escape id>`, so
    /// a rerun overwrites the same files. Other `f-*.json` and `e-*.json` files in the
    /// directory are removed, so it is a snapshot of this run (filtered by `--since` and
    /// `--repo` when given). Refuses an output directory inside a git checkout, with a
    /// `..` component, or non-empty without case files. Prints the case count, the
    /// removed count, and the skips per reason.
    ExportBenchCases {
        /// Output directory, outside every git checkout (created with mode 0700)
        #[arg(long)]
        out: std::path::PathBuf,
        /// Only findings logged and escapes recorded within this duration (e.g. "90d")
        #[arg(long)]
        since: Option<String>,
        /// Only records whose repository path is PATH
        #[arg(long, value_name = "PATH")]
        repo: Option<String>,
    },
}

#[derive(Subcommand)]
pub enum RedactAction {
    /// Test a string against redaction patterns
    Test {
        /// Input string to test
        input: String,
    },
}

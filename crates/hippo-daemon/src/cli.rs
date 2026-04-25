use clap::{Parser, Subcommand};

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
        /// Run only the named source probe (shell, claude-tool, claude-session, browser).
        /// Omit to run all probes.
        #[arg(long)]
        source: Option<String>,
    },
    /// Capture-reliability watchdog (invoked by launchd every 60 s)
    Watchdog {
        #[command(subcommand)]
        action: WatchdogAction,
    },
}

#[derive(Subcommand)]
pub enum WatchdogAction {
    /// Assert invariants against source_health and write capture_alarms rows.
    /// Designed to be invoked by launchd; exits 0 on success.
    Run,
}

#[derive(Subcommand)]
pub enum BrainAction {
    /// Stop the brain server (sends SIGTERM)
    Stop,
}

#[derive(Subcommand)]
pub enum DaemonAction {
    /// Run the daemon in the foreground
    Run,
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
        /// Captured stdout+stderr (truncated)
        #[arg(long)]
        output: Option<String>,
        /// Probe tag UUID: marks this event as a synthetic probe.
        /// Set to the same UUID as envelope_id. Excluded from all user queries.
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
        /// Batch mode: process entire file and exit (default: tail mode)
        #[arg(long)]
        batch: bool,
        /// Run inline instead of spawning a tmux window (used internally)
        #[arg(long)]
        inline: bool,
        /// Wait up to N seconds for the file to appear before tailing (default: 0 = no wait)
        #[arg(long, default_value_t = 0)]
        wait_for_file: u64,
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

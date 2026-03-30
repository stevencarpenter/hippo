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
    /// Run diagnostic checks
    Doctor,
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
        /// Git branch
        #[arg(long)]
        git_branch: Option<String>,
        /// Git commit hash
        #[arg(long)]
        git_commit: Option<String>,
        /// Git dirty flag
        #[arg(long)]
        git_dirty: bool,
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

# Shell Integration

Zsh hooks that capture shell activity and send events to the Hippo daemon.

## Files

| File            | Purpose                                                                                                                         |
|-----------------|---------------------------------------------------------------------------------------------------------------------------------|
| `hippo.zsh`     | Main hook — `preexec` captures the command, `precmd` sends the event to the daemon with exit code, duration, cwd, and git state |
| `hippo-env.zsh` | Source from `.zshenv` — generates a stable `HIPPO_SESSION_ID` per login session                                                 |

## Setup

Add to your `.zshenv`:

```zsh
source /path/to/hippo/shell/hippo-env.zsh
```

Add to your `.zshrc`:

```zsh
source /path/to/hippo/shell/hippo.zsh
```

## How It Works

1. `preexec` fires before each command — saves the command string, cwd, and start time
2. `precmd` fires after the command completes — computes duration, captures exit code and git state, then sends the
   event to the daemon via `hippo send-event shell`
3. Events are sent fire-and-forget in the background so shell responsiveness is unaffected

## Opt-in stdout and stderr

In an interactive zsh session, wrap a noninteractive command to capture both streams:

```zsh
hippo_capture cargo test
```

The wrapper copies stdout and stderr to their original destinations and separate private files. It returns the command's exit status. The background sender applies the configured `[daemon] output_head_lines` and `output_tail_lines` limits independently, then removes the files. Redaction applies to both streams before storage.

Ordinary commands keep their TTY behavior and capture only command metadata. Do not wrap editors, pagers, REPLs, or other terminal-dependent programs. Wrapped commands see pipes instead of terminal descriptors. To capture a pipeline, pass it to an explicit noninteractive shell:

```zsh
hippo_capture zsh -fc 'producer | consumer'
```

The existing `hippo send-event shell --output ...` path still stores legacy single-stream output as stdout.

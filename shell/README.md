# Shell Integration

Zsh hooks that capture shell activity and send events to the Hippo daemon.

## Files

| File | Purpose |
|---|---|
| `hippo.zsh` | Main hook — `preexec` captures the command, `precmd` sends the event to the daemon with exit code, duration, cwd, and git state |
| `hippo-env.zsh` | Source from `.zshenv` — generates a stable `HIPPO_SESSION_ID` per login session |

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
2. `precmd` fires after the command completes — computes duration, captures exit code and git state, then sends the event to the daemon via `hippo send-event shell`
3. Events are sent fire-and-forget in the background so shell responsiveness is unaffected

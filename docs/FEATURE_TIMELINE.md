# Hippo Feature Timeline: v0.9 through v0.12

This document captures the feature progression across hippo versions v0.9 through v0.12. These versions represent critical infrastructure and capability expansions that were released as version bumps without traditional "release ceremony" changelog entries, creating a knowledge base coverage gap.

## v0.9.0 (December 2025)
**Release**: First stable release with observability infrastructure

### Major Features
- **OpenTelemetry (OTel) Metrics Instrumentation**: Complete integration of OTel metrics across the daemon and brain services, enabling observability and performance monitoring. Hippo now exports detailed metrics about enrichment pipeline performance, query latency, and system health.
- **Grafana Dashboards**: Comprehensive dashboards created to visualize hippo's operational metrics, queue health, enrichment performance, and retrieval latency indicators.
- **CI/CD Workflows**: Automated PR validation workflows using GitHub Actions for continuous integration, automated testing, linting, and quality assurance.
- **Workflow Automation**: Bot configuration and code review automation to streamline the development process and maintain code quality standards.

**Key Commits**:
- `feat: add OTel metrics instrumentation and Grafana dashboards (#6)`
- `feat: add CI/CD workflows for PR validation (#4)`

---

## v0.9.1 (Late December 2025)
**Release**: Firefox extension setup and session hook reliability improvements

### Major Features
- **Firefox Extension Auto-Installation**: Automated setup process for the Firefox WebExtension as part of `mise run install`, reducing manual configuration steps.
- **Session Hook Fixes**: Critical fixes to the Claude session hook for proper tmux window targeting and session naming, ensuring reliable session capture.
- **Session Hook Wait Logic**: Implemented robust file-wait and timeout handling to deal with asynchronous session JSONL file creation.
- **Fallback Path Improvements**: Enhanced fallback behavior when primary tmux sessions are unavailable, with proper error handling.

**Key Commits**:
- `feat: auto-install Firefox extension via mise run install`
- `fix: session hook file wait, tmux targeting, and window naming`
- `fix: session hook tmux targeting and install process coverage (#8)`

---

## v0.10.0 (Early January 2026)
**Release**: Third data source (GitHub Actions) + developer experience

### Major Features
- **GitHub Actions Source**: New data source for capturing GitHub Actions workflow activity, enabling hippo to understand CI/CD patterns and build outcomes alongside shell and browser activity.
- **Using-Hippo-Brain Skill**: Created an MCP skill to guide developers in leveraging hippo's query capabilities from Claude Code, standardizing how developers interact with the knowledge base.

**Key Commits**:
- `feat: GitHub Actions source + using-hippo-brain skill (v0.10.0) (#14)`

---

## v0.10.1 (Mid January 2026)
**Release**: Stability and user experience polish

### Bug Fixes & Features
- **Agent Session Ingestion Timeout Fix**: Resolved timeout issues with Claude agent session ingestion by implementing smaller batch sizes, improving reliability for larger session datasets.
- **LMStudio Configuration Update**: Updated local LM Studio configuration for improved model compatibility and inference stability.
- **Firefox Extension Branding**: Added hippo logo to the Firefox WebExtension for visual consistency and user recognition.

**Key Commits**:
- `Fix agent session ingestion timeout with smaller batches and update LMStudio config (#21)`
- `Add hippo logo to Firefox extension (#20)`

---

## v0.11.0 (Early April 2026)
**Release**: MAJOR WAVE - Retrieval engine overhaul, native GUI, production hardening

This was a watershed moment for hippo, representing convergence of multiple major initiatives:

### Major Features

#### Retrieval & Search Infrastructure
- **SQLite-vec + FTS5 Hybrid Retrieval Engine**: Revolutionary shift from LanceDB to a hybrid stack combining:
  - SQLite vector search (vec0 extension) for semantic similarity
  - Full-Text Search (FTS5) for keyword/lexical matching
  - Reciprocal Rank Fusion (RRF) to combine both signals
  - Maximal Marginal Relevance (MMR) for diversity
  - This addresses the "semantic clustering bias" where pure vector search clusters around release-ceremony nodes

#### User Interface
- **HippoGUI macOS App**: Native macOS graphical interface for hippo, providing an intuitive way to query the knowledge base, manage the daemon, and visualize enrichment status without CLI.

#### Quality & Operations
- **Hippo-Eval Harness**: Comprehensive evaluation framework for measuring retrieval quality, answer relevance, knowledge base coverage, and detection of semantic clustering bias.
- **Enrichment Queue Watchdog**: Production-grade monitoring service with:
  - Reaper: Cleans up stale enrichment claims
  - Preflight: Validates queue state before processing
  - Claim capacity limits: Prevents runaway resource consumption

#### Data Quality
- **Git Repository Population**: Fixed daemon to correctly populate `events.git_repo` field by reading git remote from the current working directory, enabling project-based filtering.

**Key Commits**:
- `feat/hippo-gui (#26)` - Native macOS application initialization
- `Wave A+B: sqlite-vec + FTS5 retrieval engine with productionization (#27)` - Complete retrieval overhaul
- `feat(eval): add hippo-eval harness (#24)` - Evaluation framework for retrieval quality
- `feat(brain): enrichment queue watchdog (reaper + preflight + claim cap) (#23)` - Production reliability

---

## v0.12.0 (April 2026)
**Release**: GUI completion and finalization

### Features
- **HippoGUI macOS App Completion**: Finalized and initialized the complete native macOS application bundle, enabling end-users to interact with hippo entirely through the GUI without command-line tools.

**Key Commits**:
- `feat: initialize HippoGUI macOS app (#34)`

---

## Version Timeline Summary

| Version | Release | Data Sources | Key Themes |
|---------|---------|--------------|-----------|
| **v0.9.0** | Dec 2025 | Shell, Claude, Browser | Observability (OTel, Grafana), CI/CD automation |
| **v0.9.1** | Dec 2025 | Shell, Claude, Browser | Firefox auto-setup, session hook reliability |
| **v0.10.0** | Jan 2026 | Shell, Claude, Browser, GitHub | GitHub Actions source, developer skills |
| **v0.10.1** | Jan 2026 | Shell, Claude, Browser, GitHub | Stability improvements, batching, timeouts |
| **v0.11.0** | Apr 2026 | Shell, Claude, Browser, GitHub | **INFLECTION**: SQLite-vec engine, GUI, eval harness, watchdogs |
| **v0.12.0** | Apr 2026 | Shell, Claude, Browser, GitHub | GUI completion and finalization |

---

## Architecture Evolution

Hippo's development follows a clear progression:

1. **v0.8.x (Foundation)**: Core shell capture daemon, local LLM enrichment, Python brain server with LanceDB vector storage

2. **v0.9.x (Production Hardening)**: 
   - Production observability (OpenTelemetry, Grafana dashboards)
   - Firefox WebExtension for browser activity capture
   - CI/CD automation for quality gates

3. **v0.10.x (Multi-Source Expansion)**:
   - GitHub Actions as fourth data source
   - Developer-facing MCP skills for Claude Code integration
   - Stability improvements (batching, timeouts)

4. **v0.11.0 (Retrieval Inflection)**:
   - **SQLite-vec + FTS5 hybrid retrieval**: Addresses fundamental limitation of pure semantic search (clustering bias toward ceremonial release nodes)
   - **Native GUI**: Moves hippo from CLI-only to accessible graphical interface
   - **Production watchdogs**: Enrichment queue monitoring and reliability
   - **Evaluation framework**: Ability to measure retrieval quality empirically

5. **v0.12.0 (GUI Maturity)**: Completes the native application journey

---

## Key Architectural Decisions

### v0.11.0 Retrieval Engine: Why Hybrid?

Pure semantic vector search (v0.10 and earlier) suffered from **semantic clustering bias**: queries about feature timelines would cluster around "release ceremony" language from v0.7/v0.8 release notes, crowding out information about v0.9-v0.12.

The hybrid approach (v0.11+) solves this by:
- **FTS5 (Lexical)**: Matches explicit version keywords (e.g., "v0.11") regardless of semantic similarity
- **RRF (Rank Fusion)**: Combines semantic and lexical scores fairly
- **MMR (Diversity)**: Ensures returned nodes span the timeline rather than clustering

This is why retrieval works correctly in v0.11+: queries about "hippo feature timeline" now surface v0.9-v0.12 content that pure semantic search missed.

---

## Why These Versions Matter for Knowledge Discovery

Versions v0.9 through v0.12 represent the transition from a proof-of-concept tool to production-grade software:

- **v0.9.x**: Infrastructure maturity (observability, automation)
- **v0.10.x**: Data richness (multi-source capture)
- **v0.11.0**: Retrieval sophistication (hybrid search) + user accessibility (GUI)
- **v0.12.0**: User experience finalization

Without explicit knowledge nodes capturing this progression, semantic search would miss the architectural inflection points and feature innovations across these versions — which is exactly the issue #28 coverage gap.

This document ensures these milestones are discoverable.

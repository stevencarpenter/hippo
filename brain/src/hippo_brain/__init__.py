import tomllib
from pathlib import Path


def _coerce_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return default


def _default_settings() -> dict:
    data_dir = Path.home() / ".local" / "share" / "hippo"
    return {
        "db_path": str(data_dir / "hippo.db"),
        "data_dir": str(data_dir),
        "inference_base_url": "http://localhost:1234/v1",
        "inference_timeout_secs": 300.0,
        "enrichment_model": "",
        "embedding_model": "",
        "query_model": "",
        "poll_interval_secs": 5,
        "enrichment_batch_size": 10,
        "max_events_per_chunk": 10,
        "session_stale_secs": 120,
        "port": 9175,
        "telemetry_endpoint": "http://localhost:4318",
        "max_claim_batch": 10,
        "lock_timeout_secs": 600,
        "long_dwell_bypass_ms": 120_000,
        "embed_reaper_interval_secs": 300,
        "embed_reaper_batch_size": 50,
        "embed_orphan_stale_secs": 900,
    }


def _load_runtime_settings() -> dict:
    config_path = Path.home() / ".config" / "hippo" / "config.toml"
    if not config_path.exists():
        return _default_settings()

    with config_path.open("rb") as f:
        config = tomllib.load(f)

    storage = config.get("storage", {})
    data_dir = Path(
        storage.get("data_dir", Path.home() / ".local" / "share" / "hippo")
    ).expanduser()

    # Section name changed from [lmstudio] -> [inference] in the omlx
    # vendor-neutrality PR. The Rust daemon already reads [inference]; the
    # Python brain must match or it will silently fall back to defaults and
    # the enrichment loop will stall (lmstudio_reachable=false). Fail loud on
    # the legacy name rather than guessing.
    if "lmstudio" in config and "inference" not in config:
        raise RuntimeError(
            "config.toml uses the deprecated [lmstudio] section. "
            "Rename it to [inference] (the section was renamed in the "
            "omlx vendor-neutrality PR; the Rust daemon already reads it "
            "under the new name)."
        )
    inference = config.get("inference", {})
    models = config.get("models", {})
    brain = config.get("brain", {})
    telemetry = config.get("telemetry", {})
    browser = config.get("browser", {})
    reaper = config.get("reaper", {})

    return {
        "db_path": str(data_dir / "hippo.db"),
        "data_dir": str(data_dir),
        "inference_base_url": inference.get("base_url", "http://localhost:1234/v1"),
        "inference_timeout_secs": _coerce_float(inference.get("timeout_secs"), 300.0),
        "enrichment_model": models.get("enrichment", ""),
        "embedding_model": models.get("embedding", ""),
        "query_model": models.get("query", "") or models.get("enrichment", ""),
        "poll_interval_secs": brain.get("poll_interval_secs", 5),
        "enrichment_batch_size": brain.get("enrichment_batch_size", 10),
        "max_events_per_chunk": brain.get(
            "max_events_per_chunk", brain.get("enrichment_batch_size", 10)
        ),
        "session_stale_secs": brain.get("session_stale_secs", 120),
        "port": brain.get("port", 9175),
        "telemetry_endpoint": telemetry.get("endpoint", "http://localhost:4318"),
        "max_claim_batch": brain.get("max_claim_batch", 10),
        "lock_timeout_secs": brain.get("lock_timeout_secs", 600),
        "long_dwell_bypass_ms": browser.get("long_dwell_bypass_ms", 120_000),
        "embed_reaper_interval_secs": reaper.get("interval_secs", 300),
        "embed_reaper_batch_size": reaper.get("batch_size", 50),
        "embed_orphan_stale_secs": reaper.get("orphan_stale_secs", 900),
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="hippo-brain",
        description=(
            "Hippo brain — enrichment + query server. Reads settings from "
            "~/.config/hippo/config.toml and serves an HTTP API on 127.0.0.1."
        ),
    )
    parser.add_argument(
        "command",
        choices=("serve", "enrich"),
        help=(
            "serve: run the HTTP enrichment+query server (default under the "
            "LaunchAgent). enrich: run a one-shot enrichment pass "
            "(not yet implemented as a standalone command)."
        ),
    )
    args = parser.parse_args()
    command = args.command

    if command == "serve":
        import uvicorn

        from hippo_brain.server import create_app
        from hippo_brain.telemetry import init_telemetry

        settings = _load_runtime_settings()

        # Brain uses HTTP OTLP (port 4318); config.toml stores the daemon's gRPC
        # endpoint (4317) as the single [telemetry] endpoint key. This replace
        # is a local-stack convenience — if you run a remote collector with a
        # non-standard port, set OTEL_EXPORTER_OTLP_ENDPOINT=http://host:PORT
        # in the brain LaunchAgent env to bypass this substitution.
        otel_endpoint = settings.get("telemetry_endpoint", "").replace(":4317", ":4318")
        _otel_shutdown = init_telemetry("hippo-brain", endpoint=otel_endpoint)

        app = create_app(
            db_path=settings["db_path"],
            data_dir=settings["data_dir"],
            inference_base_url=settings["inference_base_url"],
            inference_timeout_secs=settings["inference_timeout_secs"],
            enrichment_model=settings["enrichment_model"],
            embedding_model=settings["embedding_model"],
            query_model=settings["query_model"],
            poll_interval_secs=settings["poll_interval_secs"],
            enrichment_batch_size=settings["max_events_per_chunk"],
            session_stale_secs=settings["session_stale_secs"],
            max_claim_batch=settings["max_claim_batch"],
            lock_timeout_ms=int(settings["lock_timeout_secs"]) * 1000,
            long_dwell_bypass_ms=settings["long_dwell_bypass_ms"],
            embed_reaper_interval_secs=settings["embed_reaper_interval_secs"],
            embed_reaper_batch_size=settings["embed_reaper_batch_size"],
            embed_orphan_stale_secs=settings["embed_orphan_stale_secs"],
        )
        try:
            uvicorn.run(app, host="127.0.0.1", port=settings["port"])
        finally:
            if _otel_shutdown:
                _otel_shutdown()
    elif command == "enrich":
        print("Enrichment worker not yet implemented as standalone command.")

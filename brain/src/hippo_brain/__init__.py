import sys
import tomllib
from pathlib import Path


def _load_runtime_settings() -> dict:
    config_path = Path.home() / ".config" / "hippo" / "config.toml"
    if not config_path.exists():
        return {
            "db_path": "",
            "lmstudio_base_url": "http://localhost:1234/v1",
            "enrichment_model": "",
            "query_model": "",
            "poll_interval_secs": 5,
            "enrichment_batch_size": 30,
            "port": 9175,
        }

    with config_path.open("rb") as f:
        config = tomllib.load(f)

    storage = config.get("storage", {})
    data_dir = Path(
        storage.get("data_dir", Path.home() / ".local" / "share" / "hippo")
    ).expanduser()

    lmstudio = config.get("lmstudio", {})
    models = config.get("models", {})
    brain = config.get("brain", {})
    telemetry = config.get("telemetry", {})

    return {
        "db_path": str(data_dir / "hippo.db"),
        "data_dir": str(data_dir),
        "lmstudio_base_url": lmstudio.get("base_url", "http://localhost:1234/v1"),
        "lmstudio_timeout_secs": float(lmstudio.get("timeout_secs", 300.0)),
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
    }


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: hippo-brain <serve|enrich>")
        sys.exit(1)

    command = sys.argv[1]

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
            lmstudio_base_url=settings["lmstudio_base_url"],
            lmstudio_timeout_secs=settings["lmstudio_timeout_secs"],
            enrichment_model=settings["enrichment_model"],
            embedding_model=settings["embedding_model"],
            query_model=settings["query_model"],
            poll_interval_secs=settings["poll_interval_secs"],
            enrichment_batch_size=settings["max_events_per_chunk"],
            session_stale_secs=settings["session_stale_secs"],
        )
        try:
            uvicorn.run(app, host="127.0.0.1", port=settings["port"])
        finally:
            if _otel_shutdown:
                _otel_shutdown()
    elif command == "enrich":
        print("Enrichment worker not yet implemented as standalone command.")
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)

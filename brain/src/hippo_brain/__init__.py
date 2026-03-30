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
            "poll_interval_secs": 5,
            "enrichment_batch_size": 10,
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

    return {
        "db_path": str(data_dir / "hippo.db"),
        "data_dir": str(data_dir),
        "lmstudio_base_url": lmstudio.get("base_url", "http://localhost:1234/v1"),
        "enrichment_model": models.get("enrichment", ""),
        "embedding_model": models.get("embedding", ""),
        "poll_interval_secs": brain.get("poll_interval_secs", 5),
        "enrichment_batch_size": brain.get("enrichment_batch_size", 10),
        "max_events_per_chunk": brain.get("max_events_per_chunk", brain.get("enrichment_batch_size", 10)),
        "session_stale_secs": brain.get("session_stale_secs", 120),
        "port": brain.get("port", 9175),
    }


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: hippo-brain <serve|enrich>")
        sys.exit(1)

    command = sys.argv[1]

    if command == "serve":
        import uvicorn

        from hippo_brain.server import create_app

        settings = _load_runtime_settings()
        app = create_app(
            db_path=settings["db_path"],
            data_dir=settings["data_dir"],
            lmstudio_base_url=settings["lmstudio_base_url"],
            enrichment_model=settings["enrichment_model"],
            embedding_model=settings["embedding_model"],
            poll_interval_secs=settings["poll_interval_secs"],
            enrichment_batch_size=settings["max_events_per_chunk"],
            session_stale_secs=settings["session_stale_secs"],
        )
        uvicorn.run(app, host="127.0.0.1", port=settings["port"])
    elif command == "enrich":
        print("Enrichment worker not yet implemented as standalone command.")
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)

import sys


def main():
    if len(sys.argv) < 2:
        print("Usage: hippo-brain <serve|enrich>")
        sys.exit(1)

    command = sys.argv[1]

    if command == "serve":
        from hippo_brain.server import create_app
        import uvicorn

        uvicorn.run(create_app(), host="127.0.0.1", port=9175)
    elif command == "enrich":
        print("Enrichment worker not yet implemented as standalone command.")
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)

# Workflow

## Development

- Run targeted tests while iterating.
- Keep generated data outside the production database.

## Release verification

Run `mise run test` for canonical release verification. A release is not ready when only targeted tests pass.

## Publishing

Review the exact diff before staging files. Never include unrelated working-tree changes.

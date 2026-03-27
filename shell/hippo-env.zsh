# hippo-env.zsh — Source from .zshenv
# Generates a stable HIPPO_SESSION_ID per login session.
# Guards against re-setting in subshells.

if [[ -z "${HIPPO_SESSION_ID}" ]]; then
    export HIPPO_SESSION_ID="$(uuidgen)"
fi

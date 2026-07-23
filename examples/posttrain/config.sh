# Shared paths for this example. Sourced by run_native.sh / run_eval.sh /
# check_final_model.sh. EDIT THESE before running — they point outside the
# worktree on purpose (the worktree is recreated every iteration).

# TODO(setup): point this at a FIXED absolute path OUTSIDE this repo where the
#   agent saves checkpoints and final_model/. The eval reads final_model/ from here.
#   Example: /data/posttrain/gsm8k_qwen3_4b/artifacts  (NOT inside the worktree)
: "${ARTIFACTS:=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_artifacts_local}"

# TODO(setup): point this at the prebuilt env (see README SETUP). The reference
#   setup builds it once with uv and reuses it across iterations.
: "${UV_ENV:=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/uv_env}"

# Shared Hugging Face cache (so the base model + dataset aren't re-downloaded).
# TODO(setup): set to a persistent location with the model + GSM8K pre-cached.
: "${HF_HOME:=$HOME/.cache/huggingface}"

export ARTIFACTS UV_ENV HF_HOME

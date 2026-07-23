#!/bin/sh
# Source env script if provided (sets credentials, model config, etc.)
if [ -n "$AUTOHELIX_ENV_SCRIPT" ] && [ -f "$AUTOHELIX_ENV_SCRIPT" ]; then
    . "$AUTOHELIX_ENV_SCRIPT"
fi
exec autohelix "$@"

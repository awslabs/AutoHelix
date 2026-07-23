# Docker Sandbox

Run AutoHelix inside a Docker container for filesystem isolation. The agent can't access your home directory, SSH keys, AWS credentials (beyond what you explicitly provide), or other projects.

## Quick start

```bash
# 1. Build the image (one-time, takes a few minutes)
bash docker/build.sh

# 2. Create an env script with your credentials
cat > ~/autohelix-env.sh << 'EOF'
export ANTHROPIC_API_KEY=sk-ant-...
EOF

# 3. Run from your project directory
cd my-project
docker/run.sh --env ~/autohelix-env.sh run --config autohelix.yaml -n 10
```

All autohelix commands work: `run`, `report`, `clear`, `watch`.

Tip: set up an alias to avoid repeating the env path:
```bash
alias autohelix-docker='path/to/docker/run.sh --env ~/autohelix-env.sh'
autohelix-docker run -n 10
```

## What's isolated

- Agent cannot see files outside your project directory
- Agent cannot read `~/.ssh`, `~/.aws`, other projects, system files
- Container is destroyed after the run — nothing persists inside it
- Agent runs as your user (non-root), cannot escalate privileges

## What's NOT isolated

- Network: agent can reach the internet (`--network=host`). Required for LLM API calls.
- Project files: mounted read-write, so accepted iterations commit directly to your repo.

## Env script

The env script is **sourced inside the container before autohelix starts**, so it's
a general setup hook — not just a place for credentials. Any shell it runs there
happens before the first iteration. Use it for:

- Your model provider credentials — e.g. `ANTHROPIC_API_KEY`
- Bedrock configuration, if you use it (`CLAUDE_CODE_USE_BEDROCK=1`, `AWS_REGION`,
  `ANTHROPIC_MODEL`, and AWS access key / secret / session token)
- Per-run setup: install extra dependencies, pre-cache datasets or models, set
  cache locations (`HF_HOME`, `PIP_CACHE_DIR`, …), or export any other environment
  the agent needs

```bash
cat > ~/autohelix-env.sh << 'EOF'
export ANTHROPIC_API_KEY=sk-ant-...
export HF_HOME=/workspace/.hf-cache
pip install -q -r requirements.txt      # runs before autohelix starts
EOF
```

Because the container is destroyed after each run, anything installed this way is
reinstalled on the next run. For heavy dependencies you need every time, bake them
into an image instead (see [Project-specific dependencies](#project-specific-dependencies)).

Refresh credentials or setup by updating the file and re-running.

## Project-specific dependencies

The base image includes Python, pytest, git, and Claude Code. For projects with additional dependencies, extend the image:

```dockerfile
# my-project/Dockerfile.agent
FROM autohelix-agent:latest
RUN pip install numpy scipy pandas
```

Build and use:
```bash
docker build -t my-project-agent -f Dockerfile.agent .
AUTOHELIX_IMAGE=my-project-agent docker/run.sh --env ~/env.sh run -n 10
```

## Environment variables

| Variable | Purpose |
|----------|---------|
| `AUTOHELIX_IMAGE` | Override image name (default: `autohelix-agent:latest`) |

## Rebuilding the image

Rebuild after updating autohelix:
```bash
bash docker/build.sh
```

The image caches aggressively — only changed layers rebuild.

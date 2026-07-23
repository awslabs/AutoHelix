FROM python:3.12-slim

# System packages
RUN apt-get update && apt-get install -y \
    build-essential curl git \
    && rm -rf /var/lib/apt/lists/*

# Common Python tools
RUN pip install --no-cache-dir pytest

# Install Node.js (needed for Claude Code)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code
RUN npm install -g @anthropic-ai/claude-code

# Install autohelix from source
COPY . /tmp/autohelix-src/
RUN pip install --no-cache-dir /tmp/autohelix-src/ \
    && rm -rf /tmp/autohelix-src/

# Git identity for commits inside container
RUN git config --system user.email "autohelix@container" && \
    git config --system user.name "AutoHelix"

# Workspace accessible by any UID
RUN mkdir -p /workspace /home/user && chmod 777 /workspace /home/user

ENV AUTOHELIX_IN_CONTAINER=1
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /workspace
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

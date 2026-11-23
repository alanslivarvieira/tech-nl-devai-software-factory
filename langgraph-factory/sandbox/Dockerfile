# Toolchain image: used as the base of the app image AND for per-call agent sandboxes
# (FACTORY_SANDBOX=docker). Add stacks you build for (JDK/Maven, .NET SDK, Go) as needed.
FROM python:3.12-slim

ARG NODE_MAJOR=24
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# Coding-agent CLIs (headless). Authenticate via env: COPILOT_GITHUB_TOKEN / ANTHROPIC_API_KEY / OPENAI_API_KEY.
RUN npm install -g @github/copilot @anthropic-ai/claude-code @openai/codex

# Verification tooling used by the Python stack profile (others: install in a derived image).
RUN pip install --no-cache-dir pytest pytest-cov diff-cover ruff mypy hypothesis

# Factory core (tests-as-data runner, guard, gates, pymut) must be importable inside every sandbox.
COPY shared /opt/factory/shared
RUN pip install --no-cache-dir /opt/factory/shared

# Agent skills (obra/superpowers + grill-me), installed with APM and pinned by factory-skills/apm.lock.yaml.
# The factory copies them into every agent working directory (see factory_core/skills.py).
COPY factory-skills /opt/factory/factory-skills
ENV FACTORY_SKILLS_DIR=/opt/factory/factory-skills

RUN git config --system user.name "AI Software Factory" \
 && git config --system user.email "factory@users.noreply.github.com" \
 && git config --system --add safe.directory '*'
WORKDIR /work

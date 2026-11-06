# The image for the SDLC variant of the Strands Box example: the parent example's image
# (../Dockerfile) plus git-core, which the box runs as a host program for commits and pushes, and
# Claude Code, the second agent the same policy holds.
#
# A MicroVM image for the Strands Box example: agentd, Python 3.12, and the Codex Python SDK
# with its bundled Codex binary. Strands Box itself is not in the image; run.sh copies the
# binaries download-box.sh fetched into each VM, so a new Box version needs no image rebuild.
#
# The FROM must stay `public.ecr.aws/amazonlinux/amazonlinux:2023-minimal`, digest-pinned the
# same way examples/coding-agents-on-bedrock/Dockerfile pins it: microvms-core pairs that ref
# with the managed `al2023-1` base and refuses a FROM that disagrees with the baseImageArn.
#
# The agentd COPY/CMD lines mirror the client's default Dockerfile
# (crates/microvms-app/src/control/artifact.rs::default_dockerfile), which a --dockerfile build
# replaces.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:c439fb4994ea7ca529233d6256446d3f8b7b4efb58956073e015303a170011de

COPY agentd /agentd
RUN chmod 0755 /agentd

# `microvm build --project guest` puts guest/pyproject.toml and guest/uv.lock in the build
# context, and this layer installs exactly what the lockfile names: the Codex SDK
# (openai-codex) and the Codex binary it drives (openai-codex-cli-bin), both 0.163.0.
WORKDIR /opt/guest
COPY pyproject.toml uv.lock ./
ENV UV_PROJECT_ENVIRONMENT=/opt/guest/.venv \
    UV_PYTHON=python3.12 \
    UV_PYTHON_DOWNLOADS=never
RUN dnf -y --setopt=install_weak_deps=0 install python3.12 python3.12-pip tar gzip findutils procps-ng iproute git-core \
    && dnf clean all \
    && pip3.12 install --no-cache-dir uv==0.13.0 \
    && uv sync --locked \
    && chmod -R a+rX /opt/guest

# Claude Code's native Linux ARM64 binary, from the npm package the claude-code installer itself
# resolves, pinned by version and by the tarball's SHA-256 (npm's sha512 integrity for it is
# PlyxaZTA...ACg==, read 2026-11-06). Box runs it as claude-box.toml's agent.
ARG CLAUDE_CODE_VERSION=2.1.319
ARG CLAUDE_CODE_SHA256=f1ce44ea6a383ff39463e82906ad2d9d6fe1bc92976d2ac38da965b15a6f8036
RUN python3.12 -c "import hashlib, sys, urllib.request; data = urllib.request.urlopen(sys.argv[1]).read(); got = hashlib.sha256(data).hexdigest(); open('/tmp/claude-code.tgz', 'wb').write(data) if got == sys.argv[2] else sys.exit('claude-code tarball sha256 ' + got)" \
      "https://registry.npmjs.org/@anthropic-ai/claude-code-linux-arm64/-/claude-code-linux-arm64-${CLAUDE_CODE_VERSION}.tgz" \
      "$CLAUDE_CODE_SHA256" \
    && tar -xzf /tmp/claude-code.tgz -C /tmp \
    && install -D -m 0755 /tmp/package/claude /opt/claude/claude \
    && rm -rf /tmp/claude-code.tgz /tmp/package

# The user Box and the agent run as. Box contains the agent with an unprivileged user
# namespace, so nothing in the box needs root. The login shell is /sbin/nologin on purpose:
# Codex runs commands in the user's login shell when that file exists, and with no usable
# login shell it takes `bash` from PATH, where Box puts its Strands Shell alias.
RUN echo "agent:x:1000:1000::/home/agent:/sbin/nologin" >> /etc/passwd \
    && echo "agent:x:1000:" >> /etc/group \
    && mkdir -p /home/agent \
    && chown 1000:1000 /home/agent
WORKDIR /home/agent

ENV AGENTD_DEFAULT_USER=agent
ENV AGENTD_PORT=9000
ENV AGENTD_LOG=info
EXPOSE 9000
ENTRYPOINT []
CMD ["/agentd"]

# A MicroVM image for the Strands Box example: agentd, Python 3.12, and the Codex Python SDK
# with its bundled Codex binary. Strands Box itself is not in the image; run.sh copies the
# binaries build-box.sh produced into each VM, because Box publishes no Linux release yet.
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
# (openai-codex) and the Codex binary it drives (openai-codex-cli-bin), both 0.162.1.
WORKDIR /opt/guest
COPY pyproject.toml uv.lock ./
ENV UV_PROJECT_ENVIRONMENT=/opt/guest/.venv \
    UV_PYTHON=python3.12 \
    UV_PYTHON_DOWNLOADS=never
RUN dnf -y --setopt=install_weak_deps=0 install python3.12 python3.12-pip tar gzip findutils procps-ng \
    && dnf clean all \
    && pip3.12 install --no-cache-dir uv==0.13.0 \
    && uv sync --locked \
    && chmod -R a+rX /opt/guest

# The user Box and the agent run as. Box contains the agent with an unprivileged user
# namespace, so nothing in the box needs root. The login shell is /sbin/nologin on purpose:
# Codex runs commands in the user's login shell when that file exists, and with no usable
# login shell it takes `bash` from PATH, where Box puts its Strands Shell alias.
RUN echo "agent:x:1000:1000::/home/agent:/sbin/nologin" >> /etc/passwd \
    && echo "agent:x:1000:" >> /etc/group \
    && mkdir -p /home/agent \
    && chown 1000:1000 /home/agent
WORKDIR /home/agent

ENV AGENTD_PORT=9000
ENV AGENTD_LOG=info
EXPOSE 9000
ENTRYPOINT []
CMD ["/agentd"]

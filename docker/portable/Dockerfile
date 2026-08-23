# Node 24 satisfies T3's current >=24.10 requirement and both agent CLIs.
FROM node:24-bookworm

ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl gh git jq openssh-client python3 ripgrep tmux \
 && rm -rf /var/lib/apt/lists/* \
 && case "$TARGETARCH" in \
      amd64) ttyd_asset=ttyd.x86_64; ttyd_sha=8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55 ;; \
      arm64) ttyd_asset=ttyd.aarch64; ttyd_sha=b38acadd89d1d396a0f5649aa52c539edbad07f4bc7348b27b4f4b7219dd4165 ;; \
      *) echo "unsupported ttyd architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/1.7.7/$ttyd_asset" -o /usr/local/bin/ttyd \
 && echo "$ttyd_sha  /usr/local/bin/ttyd" | sha256sum -c - \
 && chmod 755 /usr/local/bin/ttyd \
 && npm install -g --allow-scripts=@anthropic-ai/claude-code,node-pty,msgpackr-extract \
      t3 @anthropic-ai/claude-code @openai/codex \
 && groupadd --gid 10001 grave \
 && useradd --uid 10001 --gid grave --create-home --shell /bin/bash grave \
 && install -d -o grave -g grave /workspace /workspace/repos /workspace/agents/t3code /workspace/config /workspace/logs

COPY --chown=grave:grave dashboard/gravedecay.py /opt/gravedecay/gravedecay.py
COPY --chown=grave:grave dashboard/static /opt/gravedecay/dashboard-static
COPY --chown=grave:grave bin/webterm /opt/gravedecay/webterm
COPY --chown=grave:grave config/tmux.conf /opt/gravedecay/tmux.conf
COPY --chown=grave:grave web/term /opt/gravedecay/web/term
COPY --chown=grave:grave docker/portable/start.sh /opt/gravedecay/start.sh
COPY docker/portable/build-term.py /opt/gravedecay/build-term.py
RUN python3 /opt/gravedecay/build-term.py /opt/gravedecay/web/term /opt/gravedecay/web/term/index.html \
 && chown grave:grave /opt/gravedecay/web/term/index.html

USER grave
WORKDIR /workspace/repos
ENV HOME=/home/grave GRAVE_ROOT=/workspace T3_BASE_DIR=/workspace/agents/t3code \
    TMUX_SOCKET=agents TMUX_CONF=/opt/gravedecay/tmux.conf \
    GRAVEDECAY_PLATFORM=container GRAVEDECAY_PORT=4712 \
    GRAVEDECAY_BASE=/grave \
    GRAVEDECAY_APPS="⌨️ T3 Code=/;🖥️ Terminal=/term/?arg=shell;🤖 Claude=/term/?arg=claude;🧠 Codex=/term/?arg=codex"

HEALTHCHECK --interval=15s --timeout=3s --retries=5 CMD curl -fsS http://127.0.0.1:4711/ >/dev/null && curl -fsS http://127.0.0.1:4712/healthz >/dev/null && curl -fsS http://127.0.0.1:4713/term/ >/dev/null || exit 1
ENTRYPOINT ["/opt/gravedecay/start.sh"]

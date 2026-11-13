# Built locally by the Supervisor, which no longer passes BUILD_FROM
# (Supervisor 2026.04): the default below is the base image. Debian 13
# (trixie), the current stable release. Pinned, so that a rebuild gives the
# same image; raise it on purpose.
ARG BUILD_FROM=ghcr.io/home-assistant/base-debian:trixie-2026.11.0
FROM ${BUILD_FROM}

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-systemd \
    && apt-get clean autoclean \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY journal2syslog.py /
COPY run.sh /

# Files copied from Windows to the addons share may lose the execute bit or
# gain CRLF line endings; either would stop the app from starting.
RUN sed -i 's/\r$//' /run.sh \
    && chmod a+x /run.sh

LABEL \
    org.opencontainers.image.title="Syslog (HL42)" \
    org.opencontainers.image.description="Send your HAOS logs to a remote syslog server" \
    org.opencontainers.image.source="https://github.com/tsukasa-HL42/ha-addon-syslog" \
    org.opencontainers.image.licenses="Apache License 2.0"

CMD ["/run.sh"]

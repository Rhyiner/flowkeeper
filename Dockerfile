FROM python:3.13-alpine

LABEL org.opencontainers.image.title="flowkeeper" \
      org.opencontainers.image.description="GitOps for n8n workflows" \
      org.opencontainers.image.licenses="MIT"

COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
RUN pip install --no-cache-dir --disable-pip-version-check /src && rm -rf /src \
    && adduser -D -u 10001 flowkeeper

USER flowkeeper
WORKDIR /work
ENTRYPOINT ["flowkeeper"]
CMD ["--help"]

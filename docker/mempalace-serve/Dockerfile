FROM python:3.12-slim

LABEL org.opencontainers.image.title="mempalace-serve"
LABEL org.opencontainers.image.description="MemPalace remote MCP server (single palace writer)"

# Pin to the host-verified version (host uv tool: 3.9.0)
RUN pip install --no-cache-dir mempalace==3.9.0

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8765

ENTRYPOINT ["docker-entrypoint.sh"]

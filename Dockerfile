# DNDMCP brain — the persistent game server, runs on a Runpod pod (always warm).
# Build amd64 for Runpod:
#   docker buildx build --platform linux/amd64 -t zackmckennarunpod/dndmcp:latest --push .
# Run on a pod, expose PORT 8000; connect harnesses to https://{podId}-8000.proxy.runpod.net,
# GUI (map + live world stream) at https://{podId}-8002.proxy.runpod.net.
# NOTE: the currently-live pod is NOT running this image — it's on runpod/base:*-cuda* via
# the live-dev SSH+git loop (see dndmcp/CLAUDE.md). Keep this Dockerfile in sync anyway; it's
# the reproducible artifact for anyone who wants a from-scratch deploy.
FROM python:3.11-slim

WORKDIR /app

# deps first for layer caching
COPY dndmcp/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# the brain
COPY dndmcp/ /app/dndmcp/

# persistent world DB lives on a mounted volume (survives restarts); default to /data
# GUI_PORT=8002, not 8001: runpod/base:*-cuda* dev images already bind nginx to 8001.
ENV DNDMCP_STATE_DIR=/data \
    DNDMCP_TRANSPORT=http \
    PORT=8000 \
    GUI_PORT=8002
VOLUME ["/data"]
EXPOSE 8000 8002

# runs MCP brain (HTTP :8000) + GUI map (:8001) together
CMD ["python", "-m", "dndmcp.app"]

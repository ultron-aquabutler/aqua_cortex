# aqua_cortex indexer image. python:3.12-slim keeps it under 200MB; no
# system deps beyond what httpx needs for HTTPS. Run with the meili_master_key
# secret mounted at /run/secrets/meili_master_key (see deploy/docker-stack.yml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Runtime deps only. We deliberately avoid tiktoken in the runtime image to
# keep the image small — the chunker uses a length-based token estimator
# (see cortex/chunker.py). If/when we move to a real tokenizer we add it here.
RUN pip install --no-cache-dir httpx==0.27.*

COPY aqua_cortex.toml ./
COPY cortex/ ./cortex/
COPY index_obsidian.py ./

# Non-root user. The meili_master_key secret is mode 0400 root:root by
# default; the indexer reads it as root inside the container.
RUN useradd --system --uid 1000 --shell /usr/sbin/nologin cortex && \
    chown -R cortex:cortex /app
USER cortex

ENTRYPOINT ["python3", "index_obsidian.py"]

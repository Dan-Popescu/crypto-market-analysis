FROM ghcr.io/astral-sh/uv:0.13.4 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
ENTRYPOINT ["uv", "run", "--no-sync", "crypto-market-analysis"]
CMD ["serve", "--transport", "stdio"]

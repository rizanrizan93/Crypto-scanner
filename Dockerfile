FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CRYPTO_SCANNER_TESTNET_EXECUTION=DISABLED

RUN groupadd --system scanner && useradd --system --gid scanner --create-home scanner

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip && python -m pip install .

USER scanner

CMD ["crypto-scanner-runtime-preflight"]

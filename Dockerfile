# T-0.DEPLOY.01 — the backend image.
#
# One container, one service: the FastAPI application, its dependencies, nothing
# else. Configuration is entirely the environment's (DATABASE_URL, and later the
# integration endpoints), so one image runs against any database without a
# rebuild, and no secret is ever copied into it.
FROM python:3.12-slim

# Containers on this host have no IPv6 route while pypi publishes AAAA records
# first, which makes `pip install` stall for minutes; prefer IPv4 in the image.
# (Runtime egress to an IPv4-only endpoint is unaffected either way.)
RUN printf 'precedence ::ffff:0:0/96  100\n' >> /etc/gai.conf

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# The pinned lock is the image's whole dependency set: same versions every build.
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY app ./app

# A runtime that cannot write to its own tree: the process runs as a plain user,
# and only the database is outside it.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin erp
USER 10001:10001

EXPOSE 8000

# Exec form: uvicorn is PID 1, so SIGTERM reaches it and the container stops
# without waiting for the kill timeout. No shell wrapper, on purpose.
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]

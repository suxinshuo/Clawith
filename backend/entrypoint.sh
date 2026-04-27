#!/bin/bash
# Docker entrypoint: run DB migrations, then start the app.
# Order matters:
#   1. alembic upgrade head — apply all migrations (creates tables + schema changes)
#   2. uvicorn — starts the FastAPI app

set -e

# --- Permission fixing and privilege dropping ---
if [ "$(id -u)" = '0' ]; then
    echo "[entrypoint] Detected root user, fixing permissions..."
    chown -R clawith:clawith ${AGENT_DATA_DIR}

    chmod -R o+rX /app

    # Grant clawith access to docker.sock for sandbox execution
    if [ -S /var/run/docker.sock ]; then
        DOCKER_GID=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || stat -f '%g' /var/run/docker.sock 2>/dev/null)
        if [ -n "$DOCKER_GID" ] && [ "$DOCKER_GID" != "0" ]; then
            groupadd -g "$DOCKER_GID" docker 2>/dev/null || true
            usermod -aG docker clawith 2>/dev/null || true
        else
            chmod 666 /var/run/docker.sock
        fi
    fi

    echo "[entrypoint] Dropping privileges to 'clawith' and re-executing..."
    exec gosu clawith /bin/bash "$0" "$@"
fi
# -------------------------------------------------------

echo "[entrypoint] Step 1: Running alembic migrations..."
# Run all migrations to ensure database schema is up to date.
# Capture exit code explicitly — do NOT let a migration failure go unnoticed.
set +e
ALEMBIC_OUTPUT=$(alembic upgrade head 2>&1)
ALEMBIC_EXIT=$?
set -e

if [ $ALEMBIC_EXIT -ne 0 ]; then
    echo ""
    echo "========================================================================"
    echo "[entrypoint] WARNING: Alembic migration FAILED (exit code $ALEMBIC_EXIT)"
    echo "========================================================================"
    echo ""
    echo "$ALEMBIC_OUTPUT"
    echo ""
    echo "------------------------------------------------------------------------"
    echo "  The database schema may be INCOMPLETE. Some features will NOT work."
    echo "  Common causes:"
    echo "    - Migration cycle detected (pull latest code to fix)"
    echo "    - Database connection issue"
    echo "    - Incompatible migration state"
    echo ""
    echo "  To fix: pull the latest code and restart the backend."
    echo "    Docker:  git pull && docker compose restart backend"
    echo "    Source:  git pull && alembic upgrade head"
    echo "------------------------------------------------------------------------"
    echo ""
    echo "[entrypoint] Continuing startup despite migration failure..."
else
    echo "[entrypoint] Alembic migrations completed successfully."
fi

echo "[entrypoint] Step 2: Starting uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000

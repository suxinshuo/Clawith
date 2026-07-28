#!/bin/bash
# Clawith — Stop Script
# Usage: ./stop.sh

set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$ROOT/.data"
PID_DIR="$DATA_DIR/pid"

BACKEND_PORT=8008
FRONTEND_PORT=3008
BACKEND_PID="$PID_DIR/backend.pid"
FRONTEND_PID="$PID_DIR/frontend.pid"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; NC='\033[0m'

# ═══════════════════════════════════════════════════════
# 加载环境变量
# ═══════════════════════════════════════════════════════
load_env() {
    if [ -f "$ROOT/.env" ]; then
        set -a
        source "$ROOT/.env"
        set +a
    fi

    : "${DATABASE_URL:=postgresql+asyncpg://clawith:clawith@localhost:5432/clawith?ssl=disable}"

    _db_hostpart=$(echo "$DATABASE_URL" | sed 's|.*://[^@]*@||' | sed 's|/.*||' | sed 's|?.*||')
    PG_HOST="${_db_hostpart%%:*}"
    PG_PORT="${_db_hostpart##*:}"
    [ "$PG_PORT" = "$PG_HOST" ] && PG_PORT="5432"
    PG_PORT=${PG_PORT:-5432}

    : "${REDIS_URL:=redis://localhost:6379/0}"

    # Tolerate an optional credential part: redis://[[user]:[pass]@]host:port/db
    _redis_hostpart=$(echo "$REDIS_URL" | sed 's|^[a-z][a-z+]*://||' | sed 's|.*@||' | sed 's|/.*||' | sed 's|?.*||')
    REDIS_HOST="${_redis_hostpart%%:*}"
    REDIS_PORT="${_redis_hostpart##*:}"
    [ "$REDIS_PORT" = "$REDIS_HOST" ] && REDIS_PORT="6379"
    REDIS_PORT=${REDIS_PORT:-6379}
}

# ═══════════════════════════════════════════════════════
# 添加 PostgreSQL 到 PATH
# ═══════════════════════════════════════════════════════
add_pg_path() {
    if [ -d "$ROOT/.pg/bin" ]; then
        export PATH="$ROOT/.pg/bin:$PATH"
    fi
    for dir in /www/server/pgsql/bin /usr/local/pgsql/bin; do
        if [ -x "$dir/pg_isready" ] && ! command -v pg_isready &>/dev/null; then
            export PATH="$dir:$PATH"
        fi
    done
}

# ═══════════════════════════════════════════════════════
# 添加 Redis 到 PATH
# ═══════════════════════════════════════════════════════
add_redis_path() {
    for dir in /opt/homebrew/opt/redis/bin /opt/homebrew/bin \
               /usr/local/opt/redis/bin /usr/local/bin; do
        if [ -x "$dir/redis-cli" ] && ! command -v redis-cli &>/dev/null; then
            export PATH="$dir:$PATH"
        fi
    done
}

# ═══════════════════════════════════════════════════════
# 停止前端和后端
# ═══════════════════════════════════════════════════════
stop_services() {
    echo -e "${YELLOW}Stopping frontend & backend...${NC}"

    for pidfile in "$BACKEND_PID" "$FRONTEND_PID"; do
        if [ -f "$pidfile" ]; then
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null || true
            fi
            rm -f "$pidfile"
        fi
    done

    for port in $BACKEND_PORT $FRONTEND_PORT; do
        if command -v lsof &>/dev/null; then
            lsof -ti:$port | xargs kill 2>/dev/null || true
        elif command -v fuser &>/dev/null; then
            fuser -k $port/tcp 2>/dev/null || true
        fi
    done

    echo -e "${GREEN}Frontend & backend stopped.${NC}"
}

# ═══════════════════════════════════════════════════════
# 停止 PostgreSQL
# ═══════════════════════════════════════════════════════
stop_postgres() {
    if [ "$PG_HOST" != "localhost" ] && [ "$PG_HOST" != "127.0.0.1" ]; then
        echo -e "${YELLOW}Using external database (${PG_HOST}) — skipping PostgreSQL stop.${NC}"
        return 0
    fi

    add_pg_path

    echo -e "${YELLOW}Stopping PostgreSQL (port $PG_PORT)...${NC}"

    # 优先使用 pg_ctl 停止本地 pgdata
    if [ -f "$ROOT/.pgdata/PG_VERSION" ] && command -v pg_ctl &>/dev/null; then
        pg_ctl -D "$ROOT/.pgdata" stop -m fast 2>/dev/null && \
            echo -e "${GREEN}PostgreSQL stopped (pg_ctl).${NC}" && return 0
    fi

    # macOS brew
    if command -v brew &>/dev/null; then
        brew services stop postgresql@15 2>/dev/null || brew services stop postgresql 2>/dev/null || true
        echo -e "${GREEN}PostgreSQL stopped (brew).${NC}" && return 0
    fi

    # Linux systemd
    if command -v systemctl &>/dev/null; then
        sudo systemctl stop postgresql 2>/dev/null || true
        echo -e "${GREEN}PostgreSQL stopped (systemctl).${NC}" && return 0
    fi

    echo -e "${RED}Could not determine how to stop PostgreSQL — please stop it manually.${NC}"
}

# ═══════════════════════════════════════════════════════
# 停止 Redis
# ═══════════════════════════════════════════════════════
stop_redis() {
    if [ "$REDIS_HOST" != "localhost" ] && [ "$REDIS_HOST" != "127.0.0.1" ]; then
        echo -e "${YELLOW}Using external Redis (${REDIS_HOST}) — skipping Redis stop.${NC}"
        return 0
    fi

    add_redis_path

    echo -e "${YELLOW}Stopping Redis (port $REDIS_PORT)...${NC}"

    # macOS brew — only when the service is actually managed by brew, otherwise
    # fall through to the shutdown path used for a standalone redis-server.
    if command -v brew &>/dev/null && brew services list 2>/dev/null | grep -qE '^redis[[:space:]]+started'; then
        brew services stop redis 2>/dev/null || true
        echo -e "${GREEN}Redis stopped (brew).${NC}" && return 0
    fi

    # Linux systemd
    if command -v systemctl &>/dev/null; then
        sudo systemctl stop redis 2>/dev/null || sudo systemctl stop redis-server 2>/dev/null || true
        echo -e "${GREEN}Redis stopped (systemctl).${NC}" && return 0
    fi

    # 由 restart.sh 直接拉起的后台 redis-server
    if command -v redis-cli &>/dev/null; then
        redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" shutdown nosave 2>/dev/null || true
        echo -e "${GREEN}Redis stopped (redis-cli shutdown).${NC}" && return 0
    fi

    echo -e "${RED}Could not determine how to stop Redis — please stop it manually.${NC}"
}

# ═══════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════
main() {
    load_env
    stop_services
    stop_postgres
    stop_redis
    echo ""
    echo -e "${GREEN}All Clawith services stopped.${NC}"
}

main

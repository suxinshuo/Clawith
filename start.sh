#!/bin/bash
# Clawith — Start Script
# Ensures PostgreSQL is running, then delegates to restart.sh

set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; NC='\033[0m'

# ═══════════════════════════════════════════════════════
# 加载环境变量
# ═══════════════════════════════════════════════════════
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

# ═══════════════════════════════════════════════════════
# 添加 PostgreSQL 到 PATH
# ═══════════════════════════════════════════════════════
if [ -d "$ROOT/.pg/bin" ]; then
    export PATH="$ROOT/.pg/bin:$PATH"
fi
for dir in /www/server/pgsql/bin /usr/local/pgsql/bin; do
    if [ -x "$dir/pg_isready" ] && ! command -v pg_isready &>/dev/null; then
        export PATH="$dir:$PATH"
    fi
done

# ═══════════════════════════════════════════════════════
# 启动 PostgreSQL
# ═══════════════════════════════════════════════════════
if [ "$PG_HOST" = "localhost" ] || [ "$PG_HOST" = "127.0.0.1" ]; then
    if command -v pg_isready &>/dev/null; then
        if pg_isready -h localhost -p "$PG_PORT" -q 2>/dev/null; then
            echo -e "${GREEN}PostgreSQL already running (port $PG_PORT)${NC}"
        else
            echo -e "${YELLOW}Starting PostgreSQL (port $PG_PORT)...${NC}"

            STARTED=false

            # 本地 pgdata 目录
            if [ -f "$ROOT/.pgdata/PG_VERSION" ] && command -v pg_ctl &>/dev/null; then
                pg_ctl -D "$ROOT/.pgdata" -l "$ROOT/.pgdata/pg.log" start >/dev/null 2>&1 && STARTED=true
            fi

            # macOS brew
            if [ "$STARTED" = false ] && command -v brew &>/dev/null; then
                brew services start postgresql@15 2>/dev/null || brew services start postgresql 2>/dev/null || true
                STARTED=true
            fi

            # Linux systemd
            if [ "$STARTED" = false ] && command -v systemctl &>/dev/null; then
                sudo systemctl start postgresql 2>/dev/null || true
                STARTED=true
            fi

            # 等待 PostgreSQL 就绪
            for i in $(seq 1 15); do
                if pg_isready -h localhost -p "$PG_PORT" -q 2>/dev/null; then
                    echo -e "${GREEN}PostgreSQL ready (${i}s)${NC}"
                    break
                fi
                if [ "$i" -eq 15 ]; then
                    echo -e "${RED}PostgreSQL failed to start on port $PG_PORT${NC}"
                    exit 1
                fi
                sleep 1
            done
        fi
    else
        echo -e "${YELLOW}pg_isready not found — assuming PostgreSQL is running${NC}"
    fi
else
    echo -e "${GREEN}Using external database at ${PG_HOST}:${PG_PORT}${NC}"
fi

# ═══════════════════════════════════════════════════════
# 启动服务
# ═══════════════════════════════════════════════════════
echo ""
exec bash "$ROOT/restart.sh" "$@"
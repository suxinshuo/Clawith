#!/bin/bash
# Clawith — Start Script
# Ensures PostgreSQL and Redis are running, then delegates to restart.sh

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

: "${REDIS_URL:=redis://localhost:6379/0}"

# Tolerate an optional credential part: redis://[[user]:[pass]@]host:port/db
_redis_hostpart=$(echo "$REDIS_URL" | sed 's|^[a-z][a-z+]*://||' | sed 's|.*@||' | sed 's|/.*||' | sed 's|?.*||')
REDIS_HOST="${_redis_hostpart%%:*}"
REDIS_PORT="${_redis_hostpart##*:}"
[ "$REDIS_PORT" = "$REDIS_HOST" ] && REDIS_PORT="6379"
REDIS_PORT=${REDIS_PORT:-6379}

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
# 添加 Redis 到 PATH
# ═══════════════════════════════════════════════════════
for dir in /opt/homebrew/opt/redis/bin /opt/homebrew/bin \
           /usr/local/opt/redis/bin /usr/local/bin; do
    if [ -x "$dir/redis-cli" ] && ! command -v redis-cli &>/dev/null; then
        export PATH="$dir:$PATH"
    fi
done

# PONG means Redis is serving; without redis-cli fall back to a plain TCP probe.
redis_ping() {
    if command -v redis-cli &>/dev/null; then
        if [ "$(redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping 2>/dev/null)" = "PONG" ]; then
            return 0
        fi
        return 1
    fi
    if (echo >/dev/tcp/"$REDIS_HOST"/"$REDIS_PORT") 2>/dev/null; then
        return 0
    fi
    if command -v nc &>/dev/null && nc -z "$REDIS_HOST" "$REDIS_PORT" 2>/dev/null; then
        return 0
    fi
    return 1
}

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
# 启动 Redis
# ═══════════════════════════════════════════════════════
# Web chat needs Redis: the WebSocket presence registry lives there, so a
# missing Redis leaves sessions stuck at "connecting" instead of failing loudly.
if [ "$REDIS_HOST" = "localhost" ] || [ "$REDIS_HOST" = "127.0.0.1" ]; then
    if redis_ping; then
        echo -e "${GREEN}Redis already running (port $REDIS_PORT)${NC}"
    else
        echo -e "${YELLOW}Starting Redis (port $REDIS_PORT)...${NC}"

        STARTED=false

        # macOS brew
        if command -v brew &>/dev/null; then
            brew services start redis 2>/dev/null || true
            STARTED=true
        fi

        # Linux systemd
        if [ "$STARTED" = false ] && command -v systemctl &>/dev/null; then
            sudo systemctl start redis 2>/dev/null || sudo systemctl start redis-server 2>/dev/null || true
            STARTED=true
        fi

        # 无服务管理器时直接起一个后台 redis-server
        if [ "$STARTED" = false ] && command -v redis-server &>/dev/null; then
            mkdir -p "$ROOT/.data/log"
            redis-server --port "$REDIS_PORT" --daemonize yes \
                --logfile "$ROOT/.data/log/redis.log" >/dev/null 2>&1 || true
            STARTED=true
        fi

        # 等待 Redis 就绪
        for i in $(seq 1 15); do
            if redis_ping; then
                echo -e "${GREEN}Redis ready (${i}s)${NC}"
                break
            fi
            if [ "$i" -eq 15 ]; then
                echo -e "${RED}Redis failed to start on port $REDIS_PORT${NC}"
                echo -e "${RED}  Install it first — macOS: brew install redis · Debian/Ubuntu: apt install redis-server${NC}"
                exit 1
            fi
            sleep 1
        done
    fi
else
    echo -e "${GREEN}Using external Redis at ${REDIS_HOST}:${REDIS_PORT}${NC}"
fi

# ═══════════════════════════════════════════════════════
# 启动服务
# ═══════════════════════════════════════════════════════
echo ""
exec bash "$ROOT/restart.sh" "$@"
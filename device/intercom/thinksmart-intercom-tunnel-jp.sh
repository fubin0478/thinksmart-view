#!/bin/bash
umask 077
# Connect directly via SSH -N -T first (lowest latency, zero proxy hairpin, works even if mihomo is stopped),
# with automatic failover to local :7890 SOCKS5 proxy if direct connection fails quickly.
# Forwards both Intercom WebSocket (:18091 -> 127.0.0.1:18091) and Device SSH (:22 -> 127.0.0.1:28022).
REMOTE_HOST="root@198.51.100.17"
ERR_FILE="/run/thinksmart-intercom-tunnel-jp.err"
UP_FILE="/run/thinksmart-intercom-tunnel-jp.up"
STREAK_FILE="/run/thinksmart-intercom-tunnel-jp.streak"
WATCH_PID=""
BACKOFF_PID=""
BASE_OPTS=(
    -N -T
    -i /etc/thinksmart-intercom/id_ed25519
    -o IdentitiesOnly=yes
    -o BatchMode=yes
    -o StrictHostKeyChecking=yes
    -o UserKnownHostsFile=/etc/thinksmart-intercom/known_hosts
    -o LogLevel=ERROR
    -o ConnectTimeout=8
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=2
    -o TCPKeepAlive=yes
    -o ExitOnForwardFailure=yes
    -p 15280
    -R 127.0.0.1:18091:127.0.0.1:18091
    -R 127.0.0.1:28022:127.0.0.1:22
)

SSH_PID=""
cleanup() {
    if [ -n "$BACKOFF_PID" ] && kill -0 "$BACKOFF_PID" 2>/dev/null; then
        kill -TERM "$BACKOFF_PID" 2>/dev/null || true
        wait "$BACKOFF_PID" 2>/dev/null || true
    fi
    if [ -n "$WATCH_PID" ] && kill -0 "$WATCH_PID" 2>/dev/null; then
        kill -TERM "$WATCH_PID" 2>/dev/null || true
        wait "$WATCH_PID" 2>/dev/null || true
    fi
    if [ -n "$SSH_PID" ] && kill -0 "$SSH_PID" 2>/dev/null; then
        pkill -TERM -P "$SSH_PID" 2>/dev/null || true
        kill -TERM "$SSH_PID" 2>/dev/null || true
        for _ in 1 2 3 4 5; do
            kill -0 "$SSH_PID" 2>/dev/null || break
            sleep 0.2
        done
        if kill -0 "$SSH_PID" 2>/dev/null; then
            pkill -KILL -P "$SSH_PID" 2>/dev/null || true
            kill -KILL "$SSH_PID" 2>/dev/null || true
        fi
        wait "$SSH_PID" 2>/dev/null || true
    fi
    rm -f "$ERR_FILE" "$UP_FILE" "$STREAK_FILE" 2>/dev/null || true
    exit 0
}
trap cleanup SIGTERM SIGINT

sleep 0.4
rm -f "$ERR_FILE" "$UP_FILE" 2>/dev/null || true
echo "[intercom-tunnel-jp] Connecting via Direct SSH to $REMOTE_HOST..." >&2
t0=$(awk '{print int($1)}' /proc/uptime)
/usr/bin/ssh "${BASE_OPTS[@]}" \
    -o PermitLocalCommand=yes \
    -o LocalCommand="cut -d. -f1 /proc/uptime > $UP_FILE" \
    "$REMOTE_HOST" 2>"$ERR_FILE" &
SSH_PID=$!
(
    SLP_PID=""
    trap '[ -n "$SLP_PID" ] && kill -TERM "$SLP_PID" 2>/dev/null || true; exit 0' TERM INT
    sleep 12 &
    SLP_PID=$!
    wait "$SLP_PID" 2>/dev/null || exit 0
    if [ ! -f "$UP_FILE" ] && [ "$(cat "/proc/$SSH_PID/comm" 2>/dev/null)" = "ssh" ] && kill -0 "$SSH_PID" 2>/dev/null; then
        echo "[intercom-tunnel-jp] Direct SSH handshake/forward stalled >12s; terminating for proxy failover..." >&2
        pkill -TERM -P "$SSH_PID" 2>/dev/null || true
        kill -TERM "$SSH_PID" 2>/dev/null || true
        sleep 1 &
        SLP_PID=$!
        wait "$SLP_PID" 2>/dev/null || exit 0
        if [ "$(cat "/proc/$SSH_PID/comm" 2>/dev/null)" = "ssh" ] && kill -0 "$SSH_PID" 2>/dev/null; then
            pkill -KILL -P "$SSH_PID" 2>/dev/null || true
            kill -KILL "$SSH_PID" 2>/dev/null || true
        fi
    fi
) &
WATCH_PID=$!
wait "$SSH_PID"
rc=$?
SSH_PID=""
if [ -n "$WATCH_PID" ]; then
    kill -TERM "$WATCH_PID" 2>/dev/null || true
    wait "$WATCH_PID" 2>/dev/null || true
    WATCH_PID=""
fi
now_u=$(cut -d. -f1 /proc/uptime)
elapsed=$(( now_u - t0 ))
was_up=0
up_elapsed=0
if [ -f "$UP_FILE" ]; then
    was_up=1
    up_t0=$(cat "$UP_FILE" 2>/dev/null || echo 0)
    [[ "$up_t0" =~ ^[0-9]+$ ]] && [ "$up_t0" -gt 0 ] && up_elapsed=$(( now_u - up_t0 ))
fi
rm -f "$UP_FILE" 2>/dev/null || true
if [ -s "$ERR_FILE" ]; then
    cat "$ERR_FILE" >&2
fi

port_held=0
if [ -s "$ERR_FILE" ] && grep -q "remote port forwarding failed" "$ERR_FILE"; then
    rm -f "$ERR_FILE" 2>/dev/null || true
    port_held=1
    echo "[intercom-tunnel-jp] Remote port still held on server (${elapsed}s); entering exponential backoff for server cleanup..." >&2
fi
rm -f "$ERR_FILE" 2>/dev/null || true

if [ "$port_held" -eq 0 ] && [ "$was_up" -eq 1 ] && [ "$up_elapsed" -ge 45 ]; then
    rm -f "$STREAK_FILE" 2>/dev/null || true
    exit "$rc"
fi

proxy_was_up=0
proxy_elapsed=0
if [ "$port_held" -eq 0 ] && ss -tln | grep -Eq ':7890([[:space:]]|$)'; then
    sleep 1.5 &
    BACKOFF_PID=$!
    wait "$BACKOFF_PID" 2>/dev/null || true
    BACKOFF_PID=""
    rm -f "$ERR_FILE" "$UP_FILE" 2>/dev/null || true
    echo "[intercom-tunnel-jp] Direct SSH exited in ${elapsed}s (rc=${rc}), falling back to local proxy :7890..." >&2
    tp0=$(awk '{print int($1)}' /proc/uptime)
    # OpenSSH automatically prepends "exec " when running ProxyCommand via $SHELL -c.
    /usr/bin/ssh "${BASE_OPTS[@]}" \
        -o PermitLocalCommand=yes \
        -o LocalCommand="cut -d. -f1 /proc/uptime > $UP_FILE" \
        -o ProxyCommand="nc -w 45 -X 5 -x 127.0.0.1:7890 %h %p" \
        "$REMOTE_HOST" 2>"$ERR_FILE" &
    SSH_PID=$!
    (
        SLP_PID=""
        trap '[ -n "$SLP_PID" ] && kill -TERM "$SLP_PID" 2>/dev/null || true; exit 0' TERM INT
        sleep 15 &
        SLP_PID=$!
        wait "$SLP_PID" 2>/dev/null || exit 0
        if [ ! -f "$UP_FILE" ] && [ "$(cat "/proc/$SSH_PID/comm" 2>/dev/null)" = "ssh" ] && kill -0 "$SSH_PID" 2>/dev/null; then
            echo "[intercom-tunnel-jp] Proxy SSH handshake/forward stalled >15s; terminating for retry..." >&2
            pkill -TERM -P "$SSH_PID" 2>/dev/null || true
            kill -TERM "$SSH_PID" 2>/dev/null || true
            sleep 1 &
            SLP_PID=$!
            wait "$SLP_PID" 2>/dev/null || exit 0
            if [ "$(cat "/proc/$SSH_PID/comm" 2>/dev/null)" = "ssh" ] && kill -0 "$SSH_PID" 2>/dev/null; then
                pkill -KILL -P "$SSH_PID" 2>/dev/null || true
                kill -KILL "$SSH_PID" 2>/dev/null || true
            fi
        fi
    ) &
    WATCH_PID=$!
    wait "$SSH_PID"
    rc=$?
    SSH_PID=""
    if [ -n "$WATCH_PID" ]; then
        kill -TERM "$WATCH_PID" 2>/dev/null || true
        wait "$WATCH_PID" 2>/dev/null || true
        WATCH_PID=""
    fi
    now_u=$(cut -d. -f1 /proc/uptime)
    proxy_elapsed=$(( now_u - tp0 ))
    proxy_up_elapsed=0
    if [ -f "$UP_FILE" ]; then
        proxy_was_up=1
        up_t0=$(cat "$UP_FILE" 2>/dev/null || echo 0)
        [[ "$up_t0" =~ ^[0-9]+$ ]] && [ "$up_t0" -gt 0 ] && proxy_up_elapsed=$(( now_u - up_t0 ))
    fi
    if [ -s "$ERR_FILE" ]; then
        cat "$ERR_FILE" >&2
    fi
    rm -f "$ERR_FILE" "$UP_FILE" 2>/dev/null || true
fi

if [ "$proxy_was_up" -eq 1 ] && [ "${proxy_up_elapsed:-0}" -ge 45 ]; then
    rm -f "$STREAK_FILE" 2>/dev/null || true
    exit "$rc"
fi

total_elapsed=$(( $(awk '{print int($1)}' /proc/uptime) - t0 ))
streak=0
if [ -f "$STREAK_FILE" ]; then
    raw_st=$(cat "$STREAK_FILE" 2>/dev/null || echo 0)
    [[ "$raw_st" =~ ^[0-9]+$ ]] && streak="$raw_st"
fi
streak=$(( streak + 1 ))
printf '%s\n' "$streak" >"$STREAK_FILE" 2>/dev/null || true
if [ "$streak" -le 1 ]; then
    backoff=10
elif [ "$streak" -eq 2 ]; then
    backoff=20
elif [ "$streak" -eq 3 ]; then
    backoff=40
else
    backoff=60
fi
echo "[intercom-tunnel-jp] Both paths failed in ${total_elapsed}s (cycle #${streak}); backing off ${backoff}s before retry..." >&2
sleep "$backoff" &
BACKOFF_PID=$!
wait "$BACKOFF_PID" 2>/dev/null || true
BACKOFF_PID=""

exit "$rc"

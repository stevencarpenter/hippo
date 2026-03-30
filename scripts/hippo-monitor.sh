#!/usr/bin/env bash
set -euo pipefail

DB=~/.local/share/hippo/hippo.db
INTERVAL=${1:-5}

ESC=$'\033'
BOLD="${ESC}[1m"
RESET="${ESC}[0m"
GREEN="${ESC}[32m"
YELLOW="${ESC}[33m"
CYAN="${ESC}[36m"
RED="${ESC}[31m"
DIM="${ESC}[90m"

while true; do
    clear
    printf '%s  hippo enrichment monitor%s  %s\n\n' "$BOLD" "$RESET" "$(date '+%H:%M:%S')"

    if [[ ! -f "$DB" ]]; then
        echo "  Database not found at $DB"
        sleep "$INTERVAL"
        continue
    fi

    # ── Queue status ──
    read -r pending processing done failed <<< "$(sqlite3 "$DB" "
        SELECT
            COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN status='processing' THEN 1 ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN status='done' THEN 1 ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0)
        FROM enrichment_queue;
    " | tr '|' ' ')"
    total=$((pending + processing + done + failed))

    printf '  %sQueue%s\n' "$BOLD" "$RESET"
    if (( total > 0 )); then
        pct=$(( done * 100 / total ))
        bar_width=50
        bar_done=$(( pct * bar_width / 100 ))
        bar_left=$(( bar_width - bar_done ))
        bar_filled=""
        bar_empty=""
        for ((i=0; i<bar_done; i++)); do bar_filled+="█"; done
        for ((i=0; i<bar_left; i++)); do bar_empty+="░"; done
        printf '  [%s%s%s%s] %d%%\n' "$GREEN" "$bar_filled" "$RESET" "$bar_empty" "$pct"
    fi
    printf '  %s✓ %s done%s  %s⏳ %s pending%s  %s⚙ %s processing%s  %s✗ %s failed%s\n\n' \
        "$GREEN" "$done" "$RESET" \
        "$YELLOW" "$pending" "$RESET" \
        "$CYAN" "$processing" "$RESET" \
        "$RED" "$failed" "$RESET"

    # ── Knowledge store ──
    VCOUNT=$(uv run --project brain python -c "
from hippo_brain.embeddings import open_vector_db, get_or_create_table
try:
    t = get_or_create_table(open_vector_db('$HOME/.local/share/hippo'))
    print(t.count_rows())
except:
    print(0)
" 2>/dev/null || echo "0")
    KCOUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM knowledge_nodes;")
    printf '  %sKnowledge%s\n' "$BOLD" "$RESET"
    printf '  %s nodes in SQLite  •  %s vectors in LanceDB\n\n' "$KCOUNT" "$VCOUNT"

    # ── Last 5 enrichments ──
    printf '  %sRecent enrichments%s\n' "$BOLD" "$RESET"
    sqlite3 -separator '|' "$DB" "
        SELECT
            CASE outcome
                WHEN 'success' THEN '✓'
                WHEN 'partial' THEN '~'
                WHEN 'failure' THEN '✗'
                ELSE '?'
            END,
            substr(embed_text, 1, 90),
            datetime(created_at/1000, 'unixepoch', 'localtime')
        FROM knowledge_nodes
        ORDER BY created_at DESC
        LIMIT 5;
    " | while IFS='|' read -r icon text ts; do
        case "$icon" in
            "✓") color="$GREEN" ;;
            "~") color="$YELLOW" ;;
            "✗") color="$RED" ;;
            *)   color="$DIM" ;;
        esac
        printf '  %s%s%s %s%s%s %s...\n' "$color" "$icon" "$RESET" "$DIM" "$ts" "$RESET" "$text"
    done

    sleep "$INTERVAL"
done

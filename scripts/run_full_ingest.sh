#!/bin/bash
# Full PDF ingestion in 500-page batches.
# Each batch saves to Postgres independently — safe to interrupt and resume.
# Already-ingested chunks are skipped automatically (content_hash dedup).
#
# Usage:
#   ./run_full_ingest.sh                    # run directly
#   nohup ./run_full_ingest.sh &            # run in background (survives terminal close)
#   systemd-inhibit --what=sleep nohup ./run_full_ingest.sh &  # background + no sleep

set -e

PYTHON="$HOME/.pyenv/versions/connected-diagnostics/bin/python"
PDF="$HOME/Downloads/2016-2021 Lexus GX460 Repair Manual (RM27D0U).pdf"
MAKE="Lexus"
MODEL="GX460"
YEAR_START=2016
YEAR_END=2021
BATCH_SIZE=500
TOTAL_PAGES=7266
LOG_DIR="$HOME/workspace/connected-diagnostics/ingest_logs"

mkdir -p "$LOG_DIR"

echo "=== Full PDF Ingestion ==="
echo "PDF: $PDF"
echo "Total pages: $TOTAL_PAGES"
echo "Batch size: $BATCH_SIZE"
echo "Log dir: $LOG_DIR"
echo ""

START=1
BATCH=1
TOTAL_BATCHES=$(( (TOTAL_PAGES + BATCH_SIZE - 1) / BATCH_SIZE ))

while [ $START -le $TOTAL_PAGES ]; do
    END=$(( START + BATCH_SIZE - 1 ))
    if [ $END -gt $TOTAL_PAGES ]; then
        END=$TOTAL_PAGES
    fi

    LOG_FILE="$LOG_DIR/batch_${BATCH}_pages_${START}-${END}.log"

    echo "[$BATCH/$TOTAL_BATCHES] Pages $START-$END ..."

    if $PYTHON -m backend.cli.ingest \
        --pdf "$PDF" \
        --make "$MAKE" --model "$MODEL" \
        --year-start $YEAR_START --year-end $YEAR_END \
        --start-page $START --end-page $END \
        2>&1 | tee "$LOG_FILE"; then
        echo "[$BATCH/$TOTAL_BATCHES] Pages $START-$END DONE"
    else
        echo "[$BATCH/$TOTAL_BATCHES] Pages $START-$END FAILED (see $LOG_FILE)"
    fi

    echo ""
    START=$(( END + 1 ))
    BATCH=$(( BATCH + 1 ))
done

echo "=== Ingestion complete ==="
echo "Logs in: $LOG_DIR"

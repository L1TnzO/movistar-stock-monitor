#!/bin/bash
# Inicia el monitor en loop. Intervalo default 90s. Ej: ./start.sh 60
INTERVAL=${1:-90}
DIR="$(cd "$(dirname "$0")" && pwd)"
# Mata instancias previas del mismo monitor (evita duplicados)
pkill -f "movistar-stock-monitor/monitor.py" 2>/dev/null
sleep 1
nohup python3 "$DIR/monitor.py" --interval "$INTERVAL" > "$DIR/monitor.out.log" 2>&1 &
echo "Monitor corriendo cada ${INTERVAL}s. PID $!. Log: $DIR/stock.log"

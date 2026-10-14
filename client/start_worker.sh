#!/bin/bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

# Jos skripti käynnistettiin tiedostoselaimesta (tuplaklikkaamalla ilman terminaalia),
# avataan se automaattisesti Konsole-päätteeseen, jotta näet lokit ja voit sulkea sen helposti.
if [ ! -t 0 ] && [ -z "$OPTIVMAF_TERMINAL" ]; then
    if command -v konsole >/dev/null 2>&1; then
        exec env OPTIVMAF_TERMINAL=1 konsole --workdir "$DIR" -e "$0"
    elif command -v alacritty >/dev/null 2>&1; then
        exec env OPTIVMAF_TERMINAL=1 alacritty --working-directory "$DIR" -e "$0"
    fi
fi

if [ ! -f "node_config.json" ]; then
    if [ -f "node_config.example.json" ]; then
        echo "[INFO] Luodaan node_config.json esimerkkipohjasta..."
        cp node_config.example.json node_config.json
    fi
fi

python3 worker.py

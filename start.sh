#!/bin/bash

echo "Käynnistetään FastAPI-taustapalvelu..."
uvicorn api.main:app --host 0.0.0.0 --port 8000 &

sleep 2

echo "Käynnistetään Worker-enkooderi etualalle..."
python3 -m workers.job_manager

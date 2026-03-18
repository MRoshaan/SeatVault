$ErrorActionPreference = "Stop"

Write-Host "Starting infrastructure (Redis + RabbitMQ)..."
docker compose up -d

Write-Host "Starting FastAPI backend on :8000..."
Start-Process powershell -ArgumentList @(
  "-NoExit",
  "-Command",
  "cd C:\lockdown; uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"
)

Write-Host "Starting Celery worker (Windows solo pool)..."
Start-Process powershell -ArgumentList @(
  "-NoExit",
  "-Command",
  "cd C:\lockdown; celery -A app.tasks.celery_app worker --loglevel=info -Q bookings,cancellations --pool=solo --concurrency=1 --without-gossip --without-mingle --without-heartbeat"
)

Write-Host "Starting Frontend (Vite dev server on :5173)..."
Start-Process powershell -ArgumentList @(
  "-NoExit",
  "-Command",
  "cd C:\lockdown\frontend; npm install; npm run dev"
)

Write-Host "All services started."

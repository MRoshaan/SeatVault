$ErrorActionPreference = "SilentlyContinue"

Write-Host "Stopping Docker infrastructure (Redis + RabbitMQ)..."
docker compose down

Write-Host "Stopping Uvicorn, Celery, and Vite processes..."
Get-Process uvicorn -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process celery -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process node -ErrorAction SilentlyContinue | Stop-Process -Force

Write-Host "All services stopped."

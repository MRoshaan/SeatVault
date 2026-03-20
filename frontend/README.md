# SeatVault Frontend

React + Vite + Tailwind + Shadcn/UI client for the SeatVault ticketing system.

## Run Frontend Only

```bash
cd C:\lockdown\frontend
npm install
npm run dev
```

Open: http://localhost:5173

## Configure API URL

Create `frontend/.env`:

```env
VITE_API_BASE_URL=http://localhost:8000
```

## Run Full Stack (Backend + Worker + Frontend)

From the repo root:

```powershell
powershell -ExecutionPolicy Bypass -File C:\lockdown\start_all.ps1
```

## Stop All Services

```powershell
powershell -ExecutionPolicy Bypass -File C:\lockdown\stop_all.ps1
```

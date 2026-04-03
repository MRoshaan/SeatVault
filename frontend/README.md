# SeatVault Frontend

React + Vite + Tailwind + Shadcn/UI client for the SeatVault ticketing system.

## Run Frontend Only

```bash
cd C:\lockdown\frontend
npm install
npm run dev
```

Open: http://localhost:5173

## Configure API URL & Auth

Create `frontend/.env`:

```env
VITE_API_BASE_URL=http://localhost:8000
VITE_AUTH_ENABLED=true
```

## Authentication (JWT)

- Login returns a signed JWT from the backend
- Token is stored in `localStorage`
- Sent with requests using:

```http
Authorization: Bearer <token>
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

## Notes

- Ensure backend is running at `http://localhost:8000`
- JWT in `localStorage` is not secure for production (use HttpOnly cookies later)
- Check `.env` and CORS if API calls fail

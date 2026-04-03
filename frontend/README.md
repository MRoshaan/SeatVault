# 🎫 SeatVault Frontend (Lockdown Edition)

A high-performance React + Vite + TypeScript client for the SeatVault ticketing system, featuring real-time seat synchronization, distributed locking visualization, and JWT-secured booking.

## 🚀 Getting Started

### 1. Prerequisites
- **Node.js** (v18+)
- **SeatVault Backend** running on port 8000
- **Redis** (for seat locking logic)

### 2. Configure Environment
Create a `.env` file in the `frontend` folder:

```env
VITE_API_BASE_URL=http://localhost:8000
VITE_AUTH_ENABLED=true

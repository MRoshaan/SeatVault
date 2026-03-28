import * as React from "react"

import SeatMap from "./SeatMap"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { useToast } from "@/components/ui/use-toast"

type AuthMode = "login" | "register"

type SessionUser = {
  id: number
  email: string
  fullName: string
}

type SessionState = {
  token: string
  user: SessionUser
}

type LoginResponse = {
  access_token: string
  user_id: number
}

type RegisterResponse = {
  email: string
  full_name: string
}

type EventResponse = {
  id: number
  name: string
  description: string | null
  venue: string
  event_date: string
  total_seats: number
  available_seats: number
}

type BookingStatusResponse = {
  task_id: string
  celery_state: string
  status: string
  booking_id?: number
  seat_id?: number
  payment_reference?: string
  confirmed_at?: string
  failure_reason?: string
}

type BookingDetailResponse = {
  id: number
  user_id: number
  seat_id: number
  status: string
  idempotency_key?: string | null
  celery_task_id?: string | null
  amount_paid?: number | null
  payment_reference?: string | null
  failure_reason?: string | null
  booked_at?: string | null
  created_at: string
  updated_at: string
}

type ServiceHealth = {
  service: string
  healthy: boolean
  latency_ms?: number
  detail?: string
}

type HealthResponse = {
  status: "healthy" | "degraded" | "unhealthy"
  version: string
  timestamp: string
  services: ServiceHealth[]
}

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000"
const SESSION_KEY = "seatvault.session"

function parseErrorMessage(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object") return fallback
  const root = payload as Record<string, unknown>
  if (typeof root.message === "string") return root.message
  if (typeof root.detail === "string") return root.detail
  if (root.detail && typeof root.detail === "object") {
    const nested = root.detail as Record<string, unknown>
    if (typeof nested.message === "string") return nested.message
  }
  return fallback
}

function prettyDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

export default function App() {
  const { toast } = useToast()

  const [mode, setMode] = React.useState<AuthMode>("login")
  const [email, setEmail] = React.useState("")
  const [password, setPassword] = React.useState("")
  const [fullName, setFullName] = React.useState("")
  const [authLoading, setAuthLoading] = React.useState(false)
  const [session, setSession] = React.useState<SessionState | null>(null)

  const [events, setEvents] = React.useState<EventResponse[]>([])
  const [eventsLoading, setEventsLoading] = React.useState(false)
  const [selectedEventId, setSelectedEventId] = React.useState<number | null>(null)

  const [newEventName, setNewEventName] = React.useState("")
  const [newEventVenue, setNewEventVenue] = React.useState("")
  const [newEventDescription, setNewEventDescription] = React.useState("")
  const [newEventDate, setNewEventDate] = React.useState("")
  const [newEventSeats, setNewEventSeats] = React.useState("100")
  const [createEventLoading, setCreateEventLoading] = React.useState(false)

  const [taskIdInput, setTaskIdInput] = React.useState("")
  const [bookingIdInput, setBookingIdInput] = React.useState("")
  const [bookingStatus, setBookingStatus] = React.useState<BookingStatusResponse | null>(null)
  const [bookingDetail, setBookingDetail] = React.useState<BookingDetailResponse | null>(null)
  const [bookingToolsLoading, setBookingToolsLoading] = React.useState(false)
  const [myBookings, setMyBookings] = React.useState<BookingDetailResponse[]>([])
  const [myBookingsLoading, setMyBookingsLoading] = React.useState(false)

  const [health, setHealth] = React.useState<HealthResponse | null>(null)
  const [healthLoading, setHealthLoading] = React.useState(false)

  React.useEffect(() => {
    const saved = localStorage.getItem(SESSION_KEY)
    if (!saved) return
    try {
      const parsed = JSON.parse(saved) as SessionState
      if (parsed?.token && parsed?.user?.id) {
        setSession(parsed)
      }
    } catch {
      localStorage.removeItem(SESSION_KEY)
    }
  }, [])

  const persistSession = React.useCallback((next: SessionState) => {
    setSession(next)
    localStorage.setItem(SESSION_KEY, JSON.stringify(next))
  }, [])

  const clearSession = React.useCallback(() => {
    setSession(null)
    localStorage.removeItem(SESSION_KEY)
    setMyBookings([])
  }, [])

  const fetchEvents = React.useCallback(async () => {
    setEventsLoading(true)
    try {
      const res = await fetch(`${API_BASE_URL}/events`)
      if (!res.ok) {
        const payload = await res.json().catch(() => null)
        throw new Error(parseErrorMessage(payload, `Failed to load events (${res.status})`))
      }
      const data: EventResponse[] = await res.json()
      setEvents(data)
      setSelectedEventId((prev) => {
        if (!data.length) return null
        if (prev && data.some((e) => e.id === prev)) return prev
        return data[0].id
      })
    } catch (error) {
      toast({
        title: "Events unavailable",
        description: error instanceof Error ? error.message : "Unexpected error",
        variant: "destructive",
      })
    } finally {
      setEventsLoading(false)
    }
  }, [toast])

  const fetchHealth = React.useCallback(async () => {
    setHealthLoading(true)
    try {
      const res = await fetch(`${API_BASE_URL}/health`)
      if (!res.ok) {
        const payload = await res.json().catch(() => null)
        throw new Error(parseErrorMessage(payload, `Failed health check (${res.status})`))
      }
      const data: HealthResponse = await res.json()
      setHealth(data)
    } catch (error) {
      toast({
        title: "Health check failed",
        description: error instanceof Error ? error.message : "Unexpected error",
        variant: "destructive",
      })
    } finally {
      setHealthLoading(false)
    }
  }, [toast])

  const fetchMyBookings = React.useCallback(async () => {
    if (!session) {
      setMyBookings([])
      return
    }

    setMyBookingsLoading(true)
    try {
      const res = await fetch(`${API_BASE_URL}/users/${session.user.id}/bookings`, {
        headers: {
          Authorization: `Bearer ${session.token}`,
        },
      })

      if (!res.ok) {
        const payload = await res.json().catch(() => null)
        throw new Error(parseErrorMessage(payload, `Failed to load bookings (${res.status})`))
      }

      const data: BookingDetailResponse[] = await res.json()
      setMyBookings(data)
    } catch (error) {
      toast({
        title: "Bookings unavailable",
        description: error instanceof Error ? error.message : "Unexpected error",
        variant: "destructive",
      })
    } finally {
      setMyBookingsLoading(false)
    }
  }, [session, toast])

  React.useEffect(() => {
    fetchEvents()
    fetchHealth()
  }, [fetchEvents, fetchHealth])

  React.useEffect(() => {
    const intervalId = setInterval(() => {
      fetchHealth()
    }, 20000)
    return () => clearInterval(intervalId)
  }, [fetchHealth])

  React.useEffect(() => {
    if (!session) {
      setMyBookings([])
      return
    }
    fetchMyBookings()
  }, [fetchMyBookings, session])

  const onRegister = React.useCallback(async () => {
    if (!fullName.trim() || !email.trim() || !password.trim()) {
      toast({
        title: "Missing fields",
        description: "Full name, email, and password are required.",
        variant: "destructive",
      })
      return
    }

    setAuthLoading(true)
    try {
      const registerRes = await fetch(`${API_BASE_URL}/users`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: email.trim(),
          full_name: fullName.trim(),
          password,
        }),
      })
      if (!registerRes.ok) {
        const payload = await registerRes.json().catch(() => null)
        throw new Error(parseErrorMessage(payload, `Registration failed (${registerRes.status})`))
      }
      const registerData: RegisterResponse = await registerRes.json()

      const loginRes = await fetch(`${API_BASE_URL}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.trim(), password }),
      })
      if (!loginRes.ok) {
        const payload = await loginRes.json().catch(() => null)
        throw new Error(parseErrorMessage(payload, `Login failed (${loginRes.status})`))
      }

      const loginData: LoginResponse = await loginRes.json()
      persistSession({
        token: loginData.access_token,
        user: {
          id: loginData.user_id,
          email: registerData.email,
          fullName: registerData.full_name,
        },
      })

      toast({
        title: "Account created",
        description: `Welcome, ${registerData.full_name}.`,
      })
      setPassword("")
    } catch (error) {
      toast({
        title: "Registration failed",
        description: error instanceof Error ? error.message : "Unexpected error",
        variant: "destructive",
      })
    } finally {
      setAuthLoading(false)
    }
  }, [email, fullName, password, persistSession, toast])

  const onLogin = React.useCallback(async () => {
    if (!email.trim() || !password.trim()) {
      toast({
        title: "Missing fields",
        description: "Email and password are required.",
        variant: "destructive",
      })
      return
    }

    setAuthLoading(true)
    try {
      const loginRes = await fetch(`${API_BASE_URL}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.trim(), password }),
      })
      if (!loginRes.ok) {
        const payload = await loginRes.json().catch(() => null)
        throw new Error(parseErrorMessage(payload, "Invalid email or password"))
      }

      const loginData: LoginResponse = await loginRes.json()
      persistSession({
        token: loginData.access_token,
        user: {
          id: loginData.user_id,
          email: email.trim(),
          fullName: fullName.trim() || email.split("@")[0],
        },
      })

      toast({ title: "Signed in", description: "JWT session is active." })
      setPassword("")
    } catch (error) {
      toast({
        title: "Login failed",
        description: error instanceof Error ? error.message : "Unexpected error",
        variant: "destructive",
      })
    } finally {
      setAuthLoading(false)
    }
  }, [email, fullName, password, persistSession, toast])

  const onCreateEvent = React.useCallback(async () => {
    if (!newEventName.trim() || !newEventVenue.trim() || !newEventDate.trim()) {
      toast({
        title: "Missing fields",
        description: "Name, venue, event date, and seats are required.",
        variant: "destructive",
      })
      return
    }

    const parsedSeats = Number(newEventSeats)
    if (!Number.isInteger(parsedSeats) || parsedSeats < 1) {
      toast({
        title: "Invalid seat count",
        description: "Total seats must be a positive integer.",
        variant: "destructive",
      })
      return
    }

    const isoDate = new Date(newEventDate)
    if (Number.isNaN(isoDate.getTime())) {
      toast({
        title: "Invalid date",
        description: "Please choose a valid date and time.",
        variant: "destructive",
      })
      return
    }

    setCreateEventLoading(true)
    try {
      const res = await fetch(`${API_BASE_URL}/events`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: newEventName.trim(),
          description: newEventDescription.trim() || null,
          venue: newEventVenue.trim(),
          event_date: isoDate.toISOString(),
          total_seats: parsedSeats,
        }),
      })

      if (!res.ok) {
        const payload = await res.json().catch(() => null)
        throw new Error(parseErrorMessage(payload, `Event creation failed (${res.status})`))
      }

      const created: EventResponse = await res.json()
      setNewEventName("")
      setNewEventVenue("")
      setNewEventDescription("")
      setNewEventDate("")
      setNewEventSeats("100")

      await fetchEvents()
      setSelectedEventId(created.id)
      toast({ title: "Event created", description: `${created.name} is now live.` })
    } catch (error) {
      toast({
        title: "Create event failed",
        description: error instanceof Error ? error.message : "Unexpected error",
        variant: "destructive",
      })
    } finally {
      setCreateEventLoading(false)
    }
  }, [fetchEvents, newEventDate, newEventDescription, newEventName, newEventSeats, newEventVenue, toast])

  const onFetchBookingStatus = React.useCallback(async () => {
    if (!taskIdInput.trim()) {
      toast({ title: "Task ID required", description: "Enter a booking task ID.", variant: "destructive" })
      return
    }
    setBookingToolsLoading(true)
    try {
      const res = await fetch(`${API_BASE_URL}/booking/status/${taskIdInput.trim()}`)
      if (!res.ok) {
        const payload = await res.json().catch(() => null)
        throw new Error(parseErrorMessage(payload, `Status lookup failed (${res.status})`))
      }
      const data: BookingStatusResponse = await res.json()
      setBookingStatus(data)
      if (data.booking_id) {
        setBookingIdInput(String(data.booking_id))
      }
    } catch (error) {
      toast({
        title: "Status lookup failed",
        description: error instanceof Error ? error.message : "Unexpected error",
        variant: "destructive",
      })
    } finally {
      setBookingToolsLoading(false)
    }
  }, [taskIdInput, toast])

  const onFetchBookingDetail = React.useCallback(async () => {
    const bookingId = bookingIdInput.trim()
    if (!bookingId) {
      toast({ title: "Booking ID required", description: "Enter a booking ID.", variant: "destructive" })
      return
    }

    setBookingToolsLoading(true)
    try {
      const res = await fetch(`${API_BASE_URL}/booking/${bookingId}`)
      if (!res.ok) {
        const payload = await res.json().catch(() => null)
        throw new Error(parseErrorMessage(payload, `Booking lookup failed (${res.status})`))
      }
      const data: BookingDetailResponse = await res.json()
      setBookingDetail(data)
    } catch (error) {
      toast({
        title: "Booking lookup failed",
        description: error instanceof Error ? error.message : "Unexpected error",
        variant: "destructive",
      })
    } finally {
      setBookingToolsLoading(false)
    }
  }, [bookingIdInput, toast])

  const onCancelBooking = React.useCallback(async (bookingIdOverride?: string) => {
    if (!session) {
      toast({ title: "Sign in required", description: "Login to cancel your booking.", variant: "destructive" })
      return
    }

    const bookingId = (bookingIdOverride ?? bookingIdInput).trim()
    if (!bookingId) {
      toast({ title: "Booking ID required", description: "Enter a booking ID.", variant: "destructive" })
      return
    }

    setBookingToolsLoading(true)
    try {
      const res = await fetch(`${API_BASE_URL}/booking/${bookingId}/cancel`, {
        method: "DELETE",
        headers: {
          Authorization: `Bearer ${session.token}`,
        },
      })

      if (!res.ok) {
        const payload = await res.json().catch(() => null)
        throw new Error(parseErrorMessage(payload, `Cancellation failed (${res.status})`))
      }

      const payload = await res.json()
      toast({
        title: "Cancellation queued",
        description: typeof payload?.message === "string" ? payload.message : `Booking ${bookingId} cancellation submitted.`,
      })
      setBookingStatus(null)
      setBookingDetail(null)
      fetchMyBookings()
      fetchEvents()
    } catch (error) {
      toast({
        title: "Cancel failed",
        description: error instanceof Error ? error.message : "Unexpected error",
        variant: "destructive",
      })
    } finally {
      setBookingToolsLoading(false)
    }
  }, [bookingIdInput, fetchEvents, fetchMyBookings, session, toast])

  const selectedEvent = React.useMemo(
    () => events.find((event) => event.id === selectedEventId) ?? null,
    [events, selectedEventId]
  )

  return (
    <div className="min-h-screen bg-[radial-gradient(circle_at_top,_rgba(12,74,110,0.14),_transparent_38%),linear-gradient(180deg,_#f8fafc_0%,_#ecfeff_42%,_#f1f5f9_100%)] text-slate-900">
      <header className="mx-auto flex w-full max-w-7xl flex-col gap-4 px-6 pt-10 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-4">
          <Badge className="rounded-full bg-cyan-700 px-4 py-1 text-[11px] uppercase tracking-[0.22em] text-white hover:bg-cyan-700">
            Full Stack Control Center
          </Badge>
          <div className="space-y-2">
            <h1 className="font-heading text-3xl tracking-tight sm:text-5xl">SeatVault Command Hub</h1>
            <p className="max-w-3xl text-sm text-slate-600 sm:text-base">
              Auth, event management, seat booking, task tracking, cancellation, and live health checks in one interface.
            </p>
          </div>
        </div>

        {session && (
          <div className="rounded-2xl border border-cyan-100 bg-white/85 px-4 py-3 text-right shadow-sm backdrop-blur">
            <p className="text-xs uppercase tracking-[0.16em] text-slate-500">Signed in</p>
            <p className="mt-1 text-sm font-semibold text-slate-900">{session.user.email}</p>
            <p className="text-xs text-slate-500">User #{session.user.id}</p>
            <Button variant="outline" size="sm" className="mt-3" onClick={clearSession}>
              Logout
            </Button>
          </div>
        )}
      </header>

      <main className="mx-auto grid w-full max-w-7xl gap-6 px-6 pb-14 pt-8 lg:grid-cols-[360px_1fr]">
        <div className="space-y-5">
          <Card className="border-slate-200 bg-white/85 shadow-sm backdrop-blur">
            <CardHeader>
              <CardTitle className="font-heading text-2xl">
                {session ? "Session Active" : mode === "login" ? "Login" : "Register"}
              </CardTitle>
              <CardDescription>
                {session
                  ? "Your JWT token is active and used for protected booking operations."
                  : "Create an account or sign in to unlock protected endpoints."}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {!session ? (
                <>
                  <div className="grid grid-cols-2 gap-2 rounded-xl bg-slate-100 p-1">
                    <button
                      type="button"
                      className={`rounded-lg px-3 py-2 text-sm font-semibold transition ${
                        mode === "login" ? "bg-white text-slate-900 shadow" : "text-slate-500"
                      }`}
                      onClick={() => setMode("login")}
                    >
                      Login
                    </button>
                    <button
                      type="button"
                      className={`rounded-lg px-3 py-2 text-sm font-semibold transition ${
                        mode === "register" ? "bg-white text-slate-900 shadow" : "text-slate-500"
                      }`}
                      onClick={() => setMode("register")}
                    >
                      Register
                    </button>
                  </div>

                  {mode === "register" && (
                    <label className="block space-y-1">
                      <span className="text-sm font-medium text-slate-700">Full name</span>
                      <input
                        value={fullName}
                        onChange={(e) => setFullName(e.target.value)}
                        placeholder="Ada Lovelace"
                        className="h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm outline-none ring-cyan-500 transition focus:ring-2"
                      />
                    </label>
                  )}

                  <label className="block space-y-1">
                    <span className="text-sm font-medium text-slate-700">Email</span>
                    <input
                      type="email"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      placeholder="you@example.com"
                      className="h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm outline-none ring-cyan-500 transition focus:ring-2"
                    />
                  </label>

                  <label className="block space-y-1">
                    <span className="text-sm font-medium text-slate-700">Password</span>
                    <input
                      type="password"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      placeholder="At least 8 characters"
                      className="h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm outline-none ring-cyan-500 transition focus:ring-2"
                    />
                  </label>

                  <Button
                    className="h-11 w-full rounded-lg bg-slate-900 text-white hover:bg-slate-800"
                    onClick={mode === "login" ? onLogin : onRegister}
                    disabled={authLoading}
                  >
                    {authLoading ? "Please wait..." : mode === "login" ? "Login" : "Create Account"}
                  </Button>
                </>
              ) : (
                <div className="space-y-2 text-sm text-slate-700">
                  <p>
                    <span className="font-semibold">Email:</span> {session.user.email}
                  </p>
                  <p>
                    <span className="font-semibold">User ID:</span> {session.user.id}
                  </p>
                  <p className="text-xs text-slate-500">Protected API calls include your Bearer token automatically.</p>
                </div>
              )}
            </CardContent>
          </Card>

          <Card className="border-slate-200 bg-white/85 shadow-sm backdrop-blur">
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between gap-2">
                <CardTitle className="font-heading text-xl">Events</CardTitle>
                <Button variant="outline" size="sm" onClick={fetchEvents} disabled={eventsLoading}>
                  {eventsLoading ? "Refreshing..." : "Refresh"}
                </Button>
              </div>
              <CardDescription>Browse active events and choose which seat map to control.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2">
              {events.length === 0 ? (
                <p className="rounded-lg border border-dashed border-slate-300 p-3 text-sm text-slate-500">
                  No active events yet.
                </p>
              ) : (
                events.map((event) => (
                  <button
                    key={event.id}
                    type="button"
                    onClick={() => setSelectedEventId(event.id)}
                    className={`w-full rounded-xl border p-3 text-left transition ${
                      selectedEventId === event.id
                        ? "border-cyan-500 bg-cyan-50"
                        : "border-slate-200 bg-white hover:border-cyan-300"
                    }`}
                  >
                    <p className="text-sm font-semibold text-slate-900">{event.name}</p>
                    <p className="mt-1 text-xs text-slate-600">{event.venue}</p>
                    <p className="mt-1 text-xs text-slate-500">{prettyDate(event.event_date)}</p>
                    <p className="mt-2 text-xs text-slate-500">
                      Available {event.available_seats} / {event.total_seats}
                    </p>
                  </button>
                ))
              )}
            </CardContent>
          </Card>

          <Card className="border-slate-200 bg-white/85 shadow-sm backdrop-blur">
            <CardHeader>
              <CardTitle className="font-heading text-xl">Create Event</CardTitle>
              <CardDescription>Use your backend admin endpoint to seed a new event instantly.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <input
                value={newEventName}
                onChange={(e) => setNewEventName(e.target.value)}
                placeholder="Event name"
                className="h-10 w-full rounded-lg border border-slate-200 px-3 text-sm outline-none ring-cyan-500 focus:ring-2"
              />
              <input
                value={newEventVenue}
                onChange={(e) => setNewEventVenue(e.target.value)}
                placeholder="Venue"
                className="h-10 w-full rounded-lg border border-slate-200 px-3 text-sm outline-none ring-cyan-500 focus:ring-2"
              />
              <input
                type="datetime-local"
                value={newEventDate}
                onChange={(e) => setNewEventDate(e.target.value)}
                className="h-10 w-full rounded-lg border border-slate-200 px-3 text-sm outline-none ring-cyan-500 focus:ring-2"
              />
              <input
                value={newEventSeats}
                onChange={(e) => setNewEventSeats(e.target.value)}
                placeholder="Total seats"
                className="h-10 w-full rounded-lg border border-slate-200 px-3 text-sm outline-none ring-cyan-500 focus:ring-2"
              />
              <textarea
                value={newEventDescription}
                onChange={(e) => setNewEventDescription(e.target.value)}
                placeholder="Description (optional)"
                className="min-h-[84px] w-full rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none ring-cyan-500 focus:ring-2"
              />
              <Button
                className="w-full"
                onClick={onCreateEvent}
                disabled={createEventLoading}
              >
                {createEventLoading ? "Creating..." : "Create Event"}
              </Button>
            </CardContent>
          </Card>
        </div>

        <div className="space-y-5">
          <Card className="border-slate-200 bg-white/85 shadow-sm backdrop-blur">
            <CardHeader className="pb-3">
              <CardTitle className="font-heading text-xl">Booking Toolkit</CardTitle>
              <CardDescription>Use every booking endpoint: task status, detail lookup, and cancellation.</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <label className="text-xs font-semibold uppercase tracking-[0.12em] text-slate-500">Task Status</label>
                <input
                  value={taskIdInput}
                  onChange={(e) => setTaskIdInput(e.target.value)}
                  placeholder="Celery task ID"
                  className="h-10 w-full rounded-lg border border-slate-200 px-3 text-sm outline-none ring-cyan-500 focus:ring-2"
                />
                <Button variant="outline" className="w-full" onClick={onFetchBookingStatus} disabled={bookingToolsLoading}>
                  Check Task
                </Button>
                {bookingStatus && (
                  <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-700">
                    <p><span className="font-semibold">Status:</span> {bookingStatus.status}</p>
                    <p><span className="font-semibold">Celery:</span> {bookingStatus.celery_state}</p>
                    {bookingStatus.booking_id && <p><span className="font-semibold">Booking ID:</span> {bookingStatus.booking_id}</p>}
                    {bookingStatus.failure_reason && <p><span className="font-semibold">Failure:</span> {bookingStatus.failure_reason}</p>}
                  </div>
                )}
              </div>

              <div className="space-y-2">
                <label className="text-xs font-semibold uppercase tracking-[0.12em] text-slate-500">Booking Detail / Cancel</label>
                <input
                  value={bookingIdInput}
                  onChange={(e) => setBookingIdInput(e.target.value)}
                  placeholder="Booking ID"
                  className="h-10 w-full rounded-lg border border-slate-200 px-3 text-sm outline-none ring-cyan-500 focus:ring-2"
                />
                <div className="grid grid-cols-2 gap-2">
                  <Button variant="outline" onClick={onFetchBookingDetail} disabled={bookingToolsLoading}>Get Detail</Button>
                  <Button variant="destructive" onClick={() => onCancelBooking()} disabled={bookingToolsLoading || !session}>Cancel</Button>
                </div>
                {bookingDetail && (
                  <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-700">
                    <p><span className="font-semibold">Status:</span> {bookingDetail.status}</p>
                    <p><span className="font-semibold">Seat:</span> {bookingDetail.seat_id}</p>
                    <p><span className="font-semibold">User:</span> {bookingDetail.user_id}</p>
                    {bookingDetail.payment_reference && <p><span className="font-semibold">Payment:</span> {bookingDetail.payment_reference}</p>}
                    <p><span className="font-semibold">Created:</span> {prettyDate(bookingDetail.created_at)}</p>
                  </div>
                )}
              </div>
            </CardContent>
          </Card>

          <Card className="border-slate-200 bg-white/85 shadow-sm backdrop-blur">
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between gap-2">
                <CardTitle className="font-heading text-xl">My Bookings</CardTitle>
                <Button variant="outline" size="sm" onClick={fetchMyBookings} disabled={!session || myBookingsLoading}>
                  {myBookingsLoading ? "Refreshing..." : "Refresh"}
                </Button>
              </div>
              <CardDescription>Powered by `GET /users/:user_id/bookings` with JWT authorization.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2">
              {!session ? (
                <p className="rounded-lg border border-dashed border-slate-300 p-3 text-sm text-slate-500">
                  Login to view your booking history.
                </p>
              ) : myBookings.length === 0 ? (
                <p className="rounded-lg border border-dashed border-slate-300 p-3 text-sm text-slate-500">
                  No bookings found for this account yet.
                </p>
              ) : (
                myBookings.slice(0, 12).map((booking) => (
                  <div key={booking.id} className="rounded-xl border border-slate-200 bg-white p-3 text-sm">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <p className="font-semibold text-slate-900">Booking #{booking.id}</p>
                        <p className="text-xs text-slate-500">Seat {booking.seat_id} • {booking.status}</p>
                        <p className="mt-1 text-xs text-slate-500">{prettyDate(booking.created_at)}</p>
                      </div>
                      <div className="flex gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            setBookingIdInput(String(booking.id))
                            setTaskIdInput(booking.celery_task_id || "")
                            setBookingDetail(booking)
                          }}
                        >
                          Select
                        </Button>
                        <Button
                          variant="destructive"
                          size="sm"
                          disabled={!session || booking.status.toLowerCase() !== "confirmed"}
                          onClick={() => {
                            setBookingIdInput(String(booking.id))
                            onCancelBooking(String(booking.id))
                          }}
                        >
                          Cancel
                        </Button>
                      </div>
                    </div>
                  </div>
                ))
              )}
            </CardContent>
          </Card>

          <Card className="border-slate-200 bg-white/85 shadow-sm backdrop-blur">
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between gap-2">
                <CardTitle className="font-heading text-xl">System Health</CardTitle>
                <Button variant="outline" size="sm" onClick={fetchHealth} disabled={healthLoading}>
                  {healthLoading ? "Checking..." : "Refresh"}
                </Button>
              </div>
              <CardDescription>Live status from `GET /health`.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              {health ? (
                <>
                  <div className="flex items-center gap-2">
                    <Badge
                      className={
                        health.status === "healthy"
                          ? "bg-emerald-600 text-white"
                          : health.status === "degraded"
                          ? "bg-amber-500 text-white"
                          : "bg-rose-600 text-white"
                      }
                    >
                      {health.status.toUpperCase()}
                    </Badge>
                    <span className="text-xs text-slate-500">{prettyDate(health.timestamp)}</span>
                  </div>
                  <div className="grid gap-2 sm:grid-cols-3">
                    {health.services.map((svc) => (
                      <div key={svc.service} className="rounded-lg border border-slate-200 bg-white p-3 text-xs text-slate-700">
                        <p className="font-semibold uppercase tracking-[0.12em] text-slate-500">{svc.service}</p>
                        <p className={svc.healthy ? "text-emerald-700" : "text-rose-700"}>
                          {svc.healthy ? "Healthy" : "Unhealthy"}
                        </p>
                        {typeof svc.latency_ms === "number" && <p>{svc.latency_ms} ms</p>}
                        {svc.detail && <p className="text-rose-700">{svc.detail}</p>}
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <p className="text-sm text-slate-500">No health data yet.</p>
              )}
            </CardContent>
          </Card>

          {selectedEventId && session ? (
            <SeatMap
              authToken={session.token}
              currentUserId={session.user.id}
              eventId={selectedEventId}
              onBookingAccepted={(bookingId, taskId) => {
                setBookingIdInput(String(bookingId))
                setTaskIdInput(taskId)
                fetchEvents()
                fetchMyBookings()
              }}
            />
          ) : (
            <Card className="border-dashed border-slate-300 bg-white/70">
              <CardContent className="py-16 text-center text-sm text-slate-500">
                {!session
                  ? "Login to start booking and cancellation actions."
                  : "Create or select an event to load the seat map."}
              </CardContent>
            </Card>
          )}

          {selectedEvent && (
            <Card className="border-cyan-100 bg-cyan-50/70">
              <CardContent className="py-4 text-sm text-slate-700">
                <span className="font-semibold">Current event:</span> {selectedEvent.name} at {selectedEvent.venue}
              </CardContent>
            </Card>
          )}
        </div>
      </main>
    </div>
  )
}

"use client"

import * as React from "react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"
import { useToast } from "@/components/ui/use-toast"
import { Loader2 } from "lucide-react"

type SeatStatus = "available" | "processing" | "booked" | "locked_other"

type Seat = {
  id: number
  status: SeatStatus
  countdown?: number
}

type SeatApi = {
  id: number
  status: "available" | "locked" | "booked" | "cancelled"
}

type SeatListResponse = {
  seats: SeatApi[]
}

type BookingAcceptedResponse = {
  task_id: string
  booking_id: number
  seat_id: number
  lock_ttl_seconds: number
  poll_url: string
}

type BookingStatusResponse = {
  status: string
  failure_reason?: string
}

type SeatInfo = {
  tier: "A Tier" | "VIP" | "GA"
  price: number
  baseClass: string
  hoverClass: string
}

function getSeatInfo(seatId: number): SeatInfo {
  if (seatId >= 1 && seatId <= 30) {
    return {
      tier: "A Tier",
      price: 120,
      baseClass: "bg-blue-500",
      hoverClass: "hover:bg-blue-600",
    }
  }
  if (seatId >= 31 && seatId <= 70) {
    return {
      tier: "VIP",
      price: 200,
      baseClass: "bg-emerald-500",
      hoverClass: "hover:bg-emerald-600",
    }
  }
  return {
    tier: "GA",
    price: 60,
    baseClass: "bg-slate-500",
    hoverClass: "hover:bg-slate-600",
  }
}

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000"
const SEATS_ENDPOINT = "/seats"
const EVENT_ID = 1
const SYNC_INTERVAL_MS = 3000

export default function SeatMap() {
  const { toast } = useToast()

  const [seats, setSeats] = React.useState<Seat[]>(
    Array.from({ length: 100 }, (_, i) => ({
      id: i + 1,
      status: "available",
    }))
  )
  const [lastUpdate, setLastUpdate] = React.useState<string | null>(null)

  const processingSeats = React.useRef<Set<number>>(new Set())
  const pollingRefs = React.useRef<Map<number, NodeJS.Timeout>>(new Map())
  const countdownRefs = React.useRef<Map<number, NodeJS.Timeout>>(new Map())

  const updateSeatStatus = React.useCallback((seatId: number, status: SeatStatus) => {
    setSeats((prev) =>
      prev.map((seat) =>
        seat.id === seatId
          ? {
              ...seat,
              status,
              countdown: status === "processing" ? seat.countdown : undefined,
            }
          : seat
      )
    )
    setLastUpdate(new Date().toLocaleTimeString())
  }, [])

  const setSeatCountdown = React.useCallback((seatId: number, seconds: number) => {
    setSeats((prev) =>
      prev.map((seat) => (seat.id === seatId ? { ...seat, countdown: seconds } : seat))
    )
  }, [])

  const clearSeatCountdown = React.useCallback((seatId: number) => {
    const timer = countdownRefs.current.get(seatId)
    if (timer) clearInterval(timer)
    countdownRefs.current.delete(seatId)

    setSeats((prev) =>
      prev.map((seat) => (seat.id === seatId ? { ...seat, countdown: undefined } : seat))
    )
  }, [])

  const startCountdown = React.useCallback(
    (seatId: number, ttlSeconds: number) => {
      clearSeatCountdown(seatId)
      setSeatCountdown(seatId, ttlSeconds)

      const timerId = setInterval(() => {
        setSeats((prev) =>
          prev.map((seat) => {
            if (seat.id !== seatId) return seat
            const next = (seat.countdown ?? ttlSeconds) - 1
            if (next <= 0) {
              clearSeatCountdown(seatId)
              return { ...seat, countdown: 0 }
            }
            return { ...seat, countdown: next }
          })
        )
      }, 1000)

      countdownRefs.current.set(seatId, timerId)
    },
    [clearSeatCountdown, setSeatCountdown]
  )

  const syncSeatsFromBackend = React.useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE_URL}${SEATS_ENDPOINT}?event_id=${EVENT_ID}`)
      if (!res.ok) return

      const data: SeatListResponse = await res.json()

      setSeats((prev) => {
        const map = new Map(prev.map((s) => [s.id, s]))

        data.seats.forEach((remoteSeat) => {
          if (processingSeats.current.has(remoteSeat.id)) return

          let status: SeatStatus = "available"
          if (remoteSeat.status === "booked") status = "booked"
          if (remoteSeat.status === "locked") status = "locked_other"

          map.set(remoteSeat.id, {
            id: remoteSeat.id,
            status,
          })
        })

        return Array.from(map.values()).sort((a, b) => a.id - b.id)
      })

      setLastUpdate(new Date().toLocaleTimeString())
    } catch {
      // silent on sync failures
    }
  }, [])

  React.useEffect(() => {
    syncSeatsFromBackend()
    const id = setInterval(syncSeatsFromBackend, SYNC_INTERVAL_MS)
    return () => clearInterval(id)
  }, [syncSeatsFromBackend])

  const pollBookingStatus = React.useCallback(
    (seatId: number, taskId: string) => {
      if (pollingRefs.current.has(seatId)) return

      const intervalId = setInterval(async () => {
        try {
          const res = await fetch(`${API_BASE_URL}/booking/status/${taskId}`)
          if (!res.ok) throw new Error(`Polling failed: ${res.status}`)

          const data: BookingStatusResponse = await res.json()

          if (data.status?.toLowerCase() === "confirmed") {
            clearInterval(intervalId)
            pollingRefs.current.delete(seatId)
            processingSeats.current.delete(seatId)
            clearSeatCountdown(seatId)

            updateSeatStatus(seatId, "booked")
            toast({
              title: "Booking Confirmed",
              description: `Seat ${seatId} is now booked.`,
              duration: 4000,
            })
          } else if (data.status?.toLowerCase() === "failed") {
            clearInterval(intervalId)
            pollingRefs.current.delete(seatId)
            processingSeats.current.delete(seatId)
            clearSeatCountdown(seatId)

            updateSeatStatus(seatId, "available")
            toast({
              title: "Booking Failed",
              description: data.failure_reason || `Seat ${seatId} is available again.`,
              variant: "destructive",
              duration: 4000,
            })
          }
        } catch {
          // keep polling on transient failures
        }
      }, 1000)

      pollingRefs.current.set(seatId, intervalId)
    },
    [clearSeatCountdown, toast, updateSeatStatus]
  )

  const handleSeatClick = async (seatId: number) => {
    const seat = seats.find((s) => s.id === seatId)
    if (!seat || seat.status !== "available") return

    processingSeats.current.add(seatId)
    updateSeatStatus(seatId, "processing")

    const idempotencyKey = crypto.randomUUID()

    try {
      const res = await fetch(`${API_BASE_URL}/book/${seatId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: 1,
          event_id: EVENT_ID,
          idempotency_key: idempotencyKey,
        }),
      })

      if (res.status === 202) {
        const data: BookingAcceptedResponse = await res.json()
        startCountdown(seatId, data.lock_ttl_seconds)
        pollBookingStatus(seatId, data.task_id)
        toast({
          title: "Booking Started",
          description: `Seat ${seatId} is being processed...`,
          duration: 2500,
        })
      } else if (res.status === 409) {
        processingSeats.current.delete(seatId)
        clearSeatCountdown(seatId)
        updateSeatStatus(seatId, "locked_other")
        toast({
          title: "Seat Unavailable",
          description: `Seat ${seatId} is currently locked or booked.`,
          variant: "destructive",
          duration: 3000,
        })
      } else {
        processingSeats.current.delete(seatId)
        clearSeatCountdown(seatId)
        updateSeatStatus(seatId, "available")
        toast({
          title: "Booking Error",
          description: `Unexpected response: ${res.status}`,
          variant: "destructive",
          duration: 3000,
        })
      }
    } catch {
      processingSeats.current.delete(seatId)
      clearSeatCountdown(seatId)
      updateSeatStatus(seatId, "available")
      toast({
        title: "Network Error",
        description: "Could not reach booking service. Try again.",
        variant: "destructive",
        duration: 3000,
      })
    }
  }

  const tooltipContent = React.useCallback((seat: Seat) => {
    if (seat.status === "processing") {
      return (
        <div className="text-xs">
          Locked by you — {typeof seat.countdown === "number" ? `${seat.countdown}s` : "processing"}
        </div>
      )
    }
    if (seat.status === "locked_other") {
      return <div className="text-xs">Reservation in progress by another user</div>
    }
    if (seat.status === "booked") {
      return <div className="text-xs">Sold Out</div>
    }

    const info = getSeatInfo(seat.id)
    return (
      <div className="text-xs space-y-1">
        <div>Tier: {info.tier}</div>
        <div>Price: ${info.price}</div>
      </div>
    )
  }, [])

  React.useEffect(() => {
    return () => {
      pollingRefs.current.forEach((intervalId) => clearInterval(intervalId))
      countdownRefs.current.forEach((intervalId) => clearInterval(intervalId))
      pollingRefs.current.clear()
      countdownRefs.current.clear()
    }
  }, [])

  const buildSeatClass = (seat: Seat) => {
    if (seat.status === "booked") {
      return "bg-pink-500 text-white opacity-100"
    }
    if (seat.status === "processing") {
      return "bg-amber-400 text-slate-900 animate-pulse"
    }
    const info = getSeatInfo(seat.id)
    return `${info.baseClass} text-white ${info.hoverClass}`
  }

  return (
    <Card className="w-full shadow-md">
      <CardHeader className="flex flex-row items-center justify-between gap-4">
        <div>
          <CardTitle className="text-xl">Live Seat Map</CardTitle>
          <CardDescription>
            Real-time sync + async booking confirmation with lock TTL countdown.
          </CardDescription>
        </div>
        <div className="flex items-center gap-3">
          <Badge className="bg-emerald-500 text-white hover:bg-emerald-500">System Live</Badge>
          {lastUpdate && (
            <span className="text-xs text-muted-foreground">Updated {lastUpdate}</span>
          )}
        </div>
      </CardHeader>

      <CardContent>
        <div className="mb-6 flex items-center justify-center">
          <div className="w-full max-w-3xl rounded-[999px] bg-slate-900/90 py-2 text-center text-xs font-semibold tracking-[0.32em] text-slate-100 shadow-inner">
            STAGE
          </div>
        </div>

        <TooltipProvider>
          <div className="grid grid-cols-5 gap-3 sm:grid-cols-5 md:grid-cols-10">
            {seats.map((seat) => (
              <Tooltip key={seat.id} delayDuration={150}>
                <TooltipTrigger asChild>
                  <Button
                    className={`h-11 w-full font-semibold ${buildSeatClass(seat)}`}
                    disabled={seat.status !== "available"}
                    onClick={() => handleSeatClick(seat.id)}
                  >
                    {seat.status === "processing" ? (
                      <span className="flex items-center gap-2">
                        <Loader2 className="h-4 w-4 animate-spin" />
                        {seat.id}
                        {typeof seat.countdown === "number" && (
                          <span className="text-xs text-slate-800">{seat.countdown}s</span>
                        )}
                      </span>
                    ) : (
                      seat.id
                    )}
                  </Button>
                </TooltipTrigger>

                <TooltipContent className="text-xs">
                  {tooltipContent(seat)}
                </TooltipContent>
              </Tooltip>
            ))}
          </div>
        </TooltipProvider>

        <div className="mt-6 grid gap-3 text-xs text-muted-foreground sm:grid-cols-5">
          <span className="flex items-center gap-2">
            <span className="h-3 w-3 rounded-full bg-emerald-500" /> VIP
          </span>
          <span className="flex items-center gap-2">
            <span className="h-3 w-3 rounded-full bg-blue-500" /> A Tier
          </span>
          <span className="flex items-center gap-2">
            <span className="h-3 w-3 rounded-full bg-slate-500" /> GA
          </span>
          <span className="flex items-center gap-2">
            <span className="h-3 w-3 rounded-full bg-amber-400" /> Processing
          </span>
          <span className="flex items-center gap-2">
            <span className="h-3 w-3 rounded-full bg-pink-500" /> Sold
          </span>
        </div>
      </CardContent>
    </Card>
  )
}

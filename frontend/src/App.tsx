import SeatMap from "./SeatMap"

export default function App() {
  return (
    <div className="min-h-screen bg-gradient-to-b from-slate-50 via-white to-slate-100 text-slate-900">
      <header className="mx-auto flex w-full max-w-5xl flex-col gap-4 px-6 pt-10">
        <div className="inline-flex w-fit items-center gap-3 rounded-full border border-slate-200 bg-white px-4 py-2 shadow-sm">
          <span className="inline-flex h-2.5 w-2.5 rounded-full bg-emerald-500" />
          <span className="text-xs font-semibold uppercase tracking-[0.22em] text-emerald-700">
            Live Platform
          </span>
        </div>
        <div className="space-y-3">
          <h1 className="text-3xl font-semibold tracking-tight text-slate-900 sm:text-4xl">
            SeatVault Live Booking Console
          </h1>
          <p className="max-w-2xl text-sm text-slate-600 sm:text-base">
            Monitor high-concurrency seat inventory in real time. Each booking request
            runs through distributed locks, asynchronous payment processing, and
            optimistic database commits.
          </p>
        </div>
      </header>

      <main className="mx-auto flex w-full max-w-5xl flex-col gap-8 px-6 py-10">
        <SeatMap />
      </main>
    </div>
  )
}

import { useState, useEffect, useCallback } from "react"

interface Job {
  id: number
  title: string
  company: string
  score: number | null
  missing_skills?: string[]
  status: "new" | "scored" | "queued" | "applying" | "applied" | "failed" | "skipped"
  scraped_at: string
}

interface Stats {
  total_jobs: number
  applied: number
  avg_score: number | null
  flagged_fields: number
}

interface Run {
  id: number
  phase: string
  log: string | null
  started_at: string
  status: "running" | "done" | "failed"
}

interface DashboardProps {
  API: string
  onJobClick: (id: number) => void
}

// STATUS_COLORS maps job statuses to colors for the status badge.
// Defined outside the component so it's not recreated on every render.
const STATUS_COLORS: Record<Job["status"], string> = {
  new:      "#6b7280",
  scored:   "#3b82f6",
  queued:   "#f59e0b",
  applying: "#8b5cf6",
  applied:  "#10b981",
  failed:   "#ef4444",
  skipped:  "#4b5563",
}

export default function Dashboard({ API, onJobClick }: DashboardProps) {
  const [stats, setStats]       = useState<Stats | null>(null)
  const [jobs, setJobs]         = useState<Job[]>([])
  const [runs, setRuns]         = useState<Run[]>([])
  const [filter, setFilter]     = useState<string>("all")
  const [loading, setLoading]   = useState<boolean>(false)
  const [running, setRunning]   = useState<boolean>(false)

  // useCallback memoizes fetchData so it doesn't change on every render.
  // Without this, any useEffect that lists fetchData as a dependency
  // would re-run on every render — an infinite loop.
  const fetchData = useCallback(async (): Promise<void> => {
    setLoading(true)
    try {
      // Parallel fetches — Promise.all fires all three at once.
      // Sequential awaits would take 3x longer.
      const [statsRes, jobsRes, runsRes] = await Promise.all([
        fetch(`${API}/stats`),
        fetch(`${API}/jobs?limit=100${filter !== "all" ? `&status=${filter}` : ""}`),
        fetch(`${API}/runs?limit=10`),
      ])
      setStats(await statsRes.json())
      setJobs(await jobsRes.json())
      setRuns(await runsRes.json())
    } finally {
      setLoading(false)
    }
  }, [API, filter])

  useEffect(() => { fetchData() }, [fetchData])

  // Trigger pipeline phase and poll for completion
  const triggerPhase = async (phase: string): Promise<void> => {
    setRunning(true)
    await fetch(`${API}/run/${phase}`, { method: "POST" })
    // Poll every 3 seconds until the latest run is no longer "running"
    const poll = setInterval(async () => {
      const res = await fetch(`${API}/runs?limit=1`)
      const [latest] = await res.json()
      if (latest?.status !== "running") {
        clearInterval(poll)
        setRunning(false)
        fetchData()
      }
    }, 3000)
  }

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: "32px 16px" }}>

      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 32 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700 }}>Job Agent</h1>
          <p style={{ margin: "4px 0 0", color: "#6b7280", fontSize: 14 }}>Autonomous application pipeline</p>
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          {["scrape", "analyse", "apply", "pipeline"].map(phase => (
            <button
              key={phase}
              onClick={() => triggerPhase(phase)}
              disabled={running}
              style={{
                padding: "8px 16px",
                borderRadius: 8,
                border: "none",
                background: running ? "#374151" : "#3b82f6",
                color: "#fff",
                cursor: running ? "not-allowed" : "pointer",
                fontWeight: 600,
                fontSize: 13,
                textTransform: "capitalize",
              }}
            >
              {running ? "Running..." : `Run ${phase}`}
            </button>
          ))}
        </div>
      </div>

      {/* Stats row */}
      {stats && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16, marginBottom: 32 }}>
          {[
            { label: "Total Jobs",   value: stats.total_jobs },
            { label: "Applied",      value: stats.applied,      color: "#10b981" },
            { label: "Avg Score",    value: stats.avg_score ? `${stats.avg_score}/100` : "—" },
            { label: "Flagged Fields", value: stats.flagged_fields, color: stats.flagged_fields > 0 ? "#ef4444" : "#e0e0e0" },
          ].map(({ label, value, color }) => (
            <div key={label} style={{ background: "#1a1a1a", borderRadius: 12, padding: "20px 24px", border: "1px solid #2a2a2a" }}>
              <p style={{ margin: 0, fontSize: 12, color: "#6b7280", textTransform: "uppercase", letterSpacing: 1 }}>{label}</p>
              <p style={{ margin: "6px 0 0", fontSize: 28, fontWeight: 700, color: color || "#e0e0e0" }}>{value ?? "—"}</p>
            </div>
          ))}
        </div>
      )}

      {/* Status filter tabs */}
      <div style={{ display: "flex", gap: 8, marginBottom: 20 }}>
        {["all", "new", "scored", "queued", "applied", "failed", "skipped"].map(s => (
          <button
            key={s}
            onClick={() => setFilter(s)}
            style={{
              padding: "6px 14px",
              borderRadius: 20,
              border: "1px solid",
              borderColor: filter === s ? "#3b82f6" : "#2a2a2a",
              background: filter === s ? "#1e3a5f" : "transparent",
              color: filter === s ? "#60a5fa" : "#9ca3af",
              cursor: "pointer",
              fontSize: 13,
              fontWeight: filter === s ? 600 : 400,
              textTransform: "capitalize",
            }}
          >
            {s}
          </button>
        ))}
      </div>

      {/* Job table */}
      <div style={{ background: "#1a1a1a", borderRadius: 12, border: "1px solid #2a2a2a", overflow: "hidden", marginBottom: 32 }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
          <thead>
            <tr style={{ borderBottom: "1px solid #2a2a2a" }}>
              {["Job", "Company", "Score", "Gaps", "Status", "Scraped"].map(h => (
                <th key={h} style={{ padding: "12px 16px", textAlign: "left", color: "#6b7280", fontWeight: 600, fontSize: 12, textTransform: "uppercase", letterSpacing: 0.5 }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={6} style={{ padding: 32, textAlign: "center", color: "#6b7280" }}>Loading...</td></tr>
            ) : jobs.length === 0 ? (
              <tr><td colSpan={6} style={{ padding: 32, textAlign: "center", color: "#6b7280" }}>No jobs found. Run the scraper to get started.</td></tr>
            ) : jobs.map((job, i) => (
              <tr
                key={job.id}
                onClick={() => onJobClick(job.id)}
                style={{
                  borderBottom: i < jobs.length - 1 ? "1px solid #1f1f1f" : "none",
                  cursor: "pointer",
                  transition: "background 0.15s",
                }}
                onMouseEnter={e => e.currentTarget.style.background = "#222"}
                onMouseLeave={e => e.currentTarget.style.background = "transparent"}
              >
                <td style={{ padding: "12px 16px", fontWeight: 500, maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{job.title}</td>
                <td style={{ padding: "12px 16px", color: "#9ca3af" }}>{job.company}</td>
                <td style={{ padding: "12px 16px" }}>
                  {job.score != null ? (
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <div style={{ width: 60, height: 6, background: "#2a2a2a", borderRadius: 3, overflow: "hidden" }}>
                        <div style={{ width: `${job.score}%`, height: "100%", background: job.score >= 70 ? "#10b981" : job.score >= 50 ? "#f59e0b" : "#ef4444", borderRadius: 3 }} />
                      </div>
                      <span style={{ fontSize: 13, color: "#9ca3af" }}>{job.score.toFixed(0)}</span>
                    </div>
                  ) : <span style={{ color: "#4b5563" }}>—</span>}
                </td>
                <td style={{ padding: "12px 16px" }}>
                  {job.missing_skills && job.missing_skills.length > 0 ? (
                    <span style={{ fontSize: 12, color: "#f59e0b" }}>{job.missing_skills.slice(0, 2).join(", ")}{job.missing_skills.length > 2 ? ` +${job.missing_skills.length - 2}` : ""}</span>
                  ) : <span style={{ color: "#4b5563" }}>—</span>}
                </td>
                <td style={{ padding: "12px 16px" }}>
                  <span style={{
                    padding: "3px 10px", borderRadius: 12, fontSize: 12, fontWeight: 600,
                    background: `${STATUS_COLORS[job.status]}22`,
                    color: STATUS_COLORS[job.status],
                  }}>{job.status}</span>
                </td>
                <td style={{ padding: "12px 16px", color: "#4b5563", fontSize: 12 }}>
                  {new Date(job.scraped_at).toLocaleDateString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Recent runs */}
      <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 12 }}>Recent Runs</h2>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {runs.map(run => (
          <div key={run.id} style={{ background: "#1a1a1a", borderRadius: 8, padding: "12px 16px", border: "1px solid #2a2a2a", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
              <span style={{ textTransform: "capitalize", fontWeight: 600, fontSize: 14 }}>{run.phase}</span>
              <span style={{ fontSize: 13, color: "#6b7280" }}>{run.log || "No log"}</span>
            </div>
            <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
              <span style={{ fontSize: 12, color: "#4b5563" }}>{new Date(run.started_at).toLocaleString()}</span>
              <span style={{
                padding: "2px 8px", borderRadius: 10, fontSize: 12, fontWeight: 600,
                background: run.status === "done" ? "#10b98122" : run.status === "running" ? "#3b82f622" : "#ef444422",
                color: run.status === "done" ? "#10b981" : run.status === "running" ? "#3b82f6" : "#ef4444",
              }}>{run.status}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
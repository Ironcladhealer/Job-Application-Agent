import { useState, useEffect} from "react"

interface Course {
  skill: string
  importance: "critical" | "important" | "nice-to-have"
  course: string
  platform: string
  url?: string
}

interface FormAttempt {
  id: number
  field_name: string
  field_value?: string
  error_detail?: string
  status: "filled" | "error" | "skipped"
}

interface JobData {
  title: string
  company: string
  location?: string
  score: number | null
  status: string
  url: string
  description?: string
  cover_letter?: string
  courses?: Course[]
}

interface JobDetailProps {
  API: string
  jobId: number | null
  onBack: () => void
}

export default function JobDetail({ API, jobId, onBack }: JobDetailProps) {
  type TabType = "overview" | "cover letter" | "gaps & courses" | "form attempts"
  
  const [job, setJob]             = useState<JobData | null>(null)
  const [attempts, setAttempts]   = useState<FormAttempt[]>([])
  const [tab, setTab]             = useState<TabType>("overview")

  useEffect(() => {
    if (!jobId) return
    Promise.all([
      fetch(`${API}/jobs/${jobId}`).then(r => r.json()),
      fetch(`${API}/jobs/${jobId}/form-attempts`).then(r => r.json()),
    ]).then(([j, a]) => { setJob(j); setAttempts(a) })
  }, [jobId, API])

  if (!job) return (
    <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: "100vh", color: "#6b7280" }}>
      Loading...
    </div>
  )

  const TABS = ["overview", "cover letter", "gaps & courses", "form attempts"] as const satisfies readonly TabType[]

  return (
    <div style={{ maxWidth: 900, margin: "0 auto", padding: "32px 16px", fontFamily: "Inter, system-ui, sans-serif" }}>

      {/* Back + header */}
      <button onClick={onBack} style={{ background: "none", border: "none", color: "#6b7280", cursor: "pointer", fontSize: 14, marginBottom: 16, padding: 0 }}>
        ← Back to dashboard
      </button>

      <div style={{ marginBottom: 24 }}>
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700 }}>{job.title}</h1>
        <p style={{ margin: "4px 0 0", color: "#9ca3af" }}>{job.company} · {job.location || "Location not specified"}</p>
        <div style={{ display: "flex", gap: 12, marginTop: 12, alignItems: "center" }}>
          {job.score != null && (
            <span style={{ fontSize: 13, color: job.score >= 70 ? "#10b981" : "#f59e0b", fontWeight: 700 }}>
              Score: {job.score.toFixed(0)}/100
            </span>
          )}
          <span style={{ padding: "2px 10px", borderRadius: 10, fontSize: 12, fontWeight: 600, background: "#1e3a5f", color: "#60a5fa" }}>
            {job.status}
          </span>
          <a href={job.url} target="_blank" rel="noreferrer" style={{ fontSize: 13, color: "#6b7280", textDecoration: "none" }}>
            View on LinkedIn ↗
          </a>
        </div>
      </div>

      {/* Tabs */}
      <div style={{ display: "flex", gap: 4, borderBottom: "1px solid #2a2a2a", marginBottom: 24 }}>
        {TABS.map(t => (
          <button key={t} onClick={() => setTab(t as TabType)} style={{
            padding: "8px 16px", border: "none", background: "none",
            color: tab === t ? "#60a5fa" : "#6b7280",
            borderBottom: tab === t ? "2px solid #3b82f6" : "2px solid transparent",
            cursor: "pointer", fontSize: 14, fontWeight: tab === t ? 600 : 400,
            textTransform: "capitalize", marginBottom: -1,
          }}>
            {t}
          </button>
        ))}
      </div>

      {/* Tab: Overview */}
      {tab === "overview" && (
        <div>
          <h3 style={{ fontSize: 15, fontWeight: 600, marginBottom: 10, color: "#9ca3af" }}>Job Description</h3>
          <div style={{ background: "#1a1a1a", borderRadius: 10, padding: 20, border: "1px solid #2a2a2a", fontSize: 14, lineHeight: 1.7, color: "#d1d5db", whiteSpace: "pre-wrap", maxHeight: 400, overflowY: "auto" }}>
            {job.description || "No description available."}
          </div>
        </div>
      )}

      {/* Tab: Cover Letter */}
      {tab === "cover letter" && (
        <div>
          <h3 style={{ fontSize: 15, fontWeight: 600, marginBottom: 10, color: "#9ca3af" }}>Generated Cover Letter</h3>
          {job.cover_letter ? (
            <div style={{ background: "#1a1a1a", borderRadius: 10, padding: 24, border: "1px solid #2a2a2a", fontSize: 14, lineHeight: 1.8, color: "#d1d5db", whiteSpace: "pre-wrap" }}>
              {job.cover_letter}
            </div>
          ) : (
            <p style={{ color: "#4b5563" }}>No cover letter generated — job may be below score threshold.</p>
          )}
        </div>
      )}

      {/* Tab: Gaps & Courses */}
      {tab === "gaps & courses" && (
        <div>
          <h3 style={{ fontSize: 15, fontWeight: 600, marginBottom: 16, color: "#9ca3af" }}>Skill Gaps & Recommended Courses</h3>
          {job.courses && job.courses.length > 0 ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {job.courses.map((gap, i) => (
                <div key={i} style={{ background: "#1a1a1a", borderRadius: 10, padding: 16, border: "1px solid #2a2a2a" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                    <div>
                      <span style={{ fontWeight: 700, fontSize: 15 }}>{gap.skill}</span>
                      <span style={{ marginLeft: 10, fontSize: 12, padding: "2px 8px", borderRadius: 8, background: gap.importance === "critical" ? "#ef444422" : "#f59e0b22", color: gap.importance === "critical" ? "#ef4444" : "#f59e0b" }}>
                        {gap.importance}
                      </span>
                    </div>
                  </div>
                  <p style={{ margin: "8px 0 4px", fontSize: 14, color: "#d1d5db" }}>{gap.course}</p>
                  <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
                    <span style={{ fontSize: 13, color: "#6b7280" }}>{gap.platform}</span>
                    {gap.url && (
                      <a href={gap.url} target="_blank" rel="noreferrer" style={{ fontSize: 13, color: "#3b82f6", textDecoration: "none" }}>
                        Open course ↗
                      </a>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p style={{ color: "#4b5563" }}>No gaps identified — strong match across all requirements.</p>
          )}
        </div>
      )}

      {/* Tab: Form Attempts */}
      {tab === "form attempts" && (
        <div>
          <h3 style={{ fontSize: 15, fontWeight: 600, marginBottom: 16, color: "#9ca3af" }}>Form Field Interactions</h3>
          {attempts.length === 0 ? (
            <p style={{ color: "#4b5563" }}>No form interactions recorded — application not yet attempted.</p>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {attempts.map(a => (
                <div key={a.id} style={{ background: "#1a1a1a", borderRadius: 8, padding: "12px 16px", border: `1px solid ${a.status === "filled" ? "#10b98133" : "#ef444433"}`, display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                  <div>
                    <span style={{ fontWeight: 600, fontSize: 14 }}>{a.field_name}</span>
                    {a.field_value && (
                      <p style={{ margin: "4px 0 0", fontSize: 13, color: "#9ca3af" }}>Filled: "{a.field_value}"</p>
                    )}
                    {a.error_detail && (
                      <p style={{ margin: "4px 0 0", fontSize: 12, color: "#ef4444" }}>{a.error_detail}</p>
                    )}
                  </div>
                  <span style={{
                    fontSize: 12, fontWeight: 600, padding: "3px 10px", borderRadius: 10,
                    background: a.status === "filled" ? "#10b98122" : "#ef444422",
                    color: a.status === "filled" ? "#10b981" : "#ef4444",
                    whiteSpace: "nowrap", marginLeft: 12,
                  }}>{a.status.replace("_", " ")}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
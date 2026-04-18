import { useState} from "react"
import Dashboard from "./pages/Dashboard"
import JobDetail from "./pages/JobDetail"

const API = "http://localhost:8000"

type Page = "dashboard" | "detail"

export default function App() {
  const [page, setPage] = useState<Page>("dashboard")
  const [selectedJobId, setSelectedJobId] = useState<number | null>(null)

  // Simple client-side routing — no React Router needed for 2 views.
  // If this grows to 5+ pages, add React Router then.
  const navigate = (to: Page, jobId: number | null = null): void => {
    setPage(to)
    setSelectedJobId(jobId)
  }

  return (
    <div style={{ fontFamily: "Inter, system-ui, sans-serif", background: "#0f0f0f", minHeight: "100vh", color: "#e0e0e0" }}>
      {page === "dashboard" && <Dashboard API={API} onJobClick={(id) => navigate("detail", id)} />}
      {page === "detail" && <JobDetail API={API} jobId={selectedJobId} onBack={() => navigate("dashboard")} />}
    </div>
  )
}
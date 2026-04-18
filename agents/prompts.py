# agents/prompts.py

# WHY LOAD THE CV AT MODULE LEVEL?
# The CV is read once when Python imports this module.
# It's injected into every prompt. If you reload it inside each
# tool call, you're hitting the filesystem 20 times per run for
# no reason. Load once, reuse everywhere.

from pathlib import Path

def _load_cv() -> str:
    path = Path("cv/cv.md")
    if not path.exists():
        raise FileNotFoundError("cv/cv.md not found. Create it before running the agent.")
    return path.read_text(encoding="utf-8")

CV_TEXT = _load_cv()

SCORING_PROMPT = """You are evaluating job-candidate fit for a software engineering role.

## Candidate CV
{cv}

## Job Listing
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

## Your Task
Score this job's fit for the candidate from 0 to 100.

Scoring breakdown (these must add up to the total score):
- skills_match     (0-40): Do the required skills appear in the CV?
- experience_match (0-30): Does the required seniority match the candidate?
- role_fit         (0-20): Is this the type of role the candidate targets?
- location_fit     (0-10): Does remote/hybrid/onsite match preferences?

Think through each dimension, then respond with ONLY this JSON — no markdown, no explanation:
{{
  "score": <int 0-100>,
  "skills_match": <int 0-40>,
  "experience_match": <int 0-30>,
  "role_fit": <int 0-20>,
  "location_fit": <int 0-10>,
  "one_line_reason": "<single sentence explaining the score>"
}}"""

GAP_ANALYSIS_PROMPT = """You are identifying skill gaps between a job posting and a candidate's CV.

## Candidate CV
{cv}

## Job Listing
Title: {title}
Description:
{description}

## Task
Identify skills the job requires that the candidate clearly lacks or has limited exposure to.
For each gap, suggest one specific, actionable course or resource.

Rules:
- Only list REAL gaps — skills genuinely absent from the CV
- Maximum 5 gaps. If fewer, that's fine. Don't pad.
- Courses must be real and findable (Udemy, Coursera, official docs, YouTube channels)
- If there are no meaningful gaps, return an empty list

Respond with ONLY this JSON — no markdown, no explanation:
{{
  "missing_skills": [
    {{
      "skill": "<skill name>",
      "importance": "critical" | "nice-to-have",
      "course": "<course name>",
      "platform": "<Udemy / Coursera / YouTube / Official Docs>",
      "url": "<direct URL if you know it, else null>"
    }}
  ]
}}"""

COVER_LETTER_PROMPT = """Write a cover letter for this job application.

## Candidate CV
{cv}

## Target Job
Title: {title}
Company: {company}
Description:
{description}

## Fit Analysis
Score: {score}/100
Strengths: {strengths}
Gaps to acknowledge: {gaps}

## Requirements
- Exactly 3 paragraphs, 250-320 words total
- Paragraph 1: Why THIS company and THIS role specifically — no generic openers
- Paragraph 2: Two or three concrete achievements from the CV most relevant to this job
- Paragraph 3: What you'd contribute in the first 90 days
- Tone: Direct and confident. No phrases like "I am excited to apply" or "I am writing to"
- Do NOT invent skills, projects, or experience not in the CV
- End with: Best regards,\\nSaim

Output only the letter text. Nothing else."""
# tools/pdf_writer.py

from pathlib import Path
from datetime import datetime
from collections import Counter
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)
from core.models import Job

REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)

async def generate_pdf_report(jobs: list[Job]) -> str:
    """
    Generates a gap analysis PDF covering all queued jobs.

    Three sections:
    1. Per-job breakdown: score, missing skills, suggested courses
    2. Skill heatmap: which skills appear most across ALL job gaps
    3. Priority learning list: the skills worth addressing first

    WHY ReportLab OVER WeasyPrint or pdfkit?
    ReportLab is pure Python — no external binaries, no Chrome, no CSS.
    WeasyPrint needs a browser engine. pdfkit needs wkhtmltopdf installed.
    On any machine with Python, ReportLab just works.
    The tradeoff: you build layout programmatically instead of with HTML/CSS.
    For structured reports (tables, sections, headings), it's the right tool.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = REPORTS_DIR / f"gap_analysis_{timestamp}.pdf"

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        rightMargin=2*cm, leftMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm,
    )

    styles = getSampleStyleSheet()

    # Custom styles — ReportLab uses named styles applied to Paragraph objects
    title_style = ParagraphStyle(
        "CustomTitle",
        parent=styles["Title"],
        fontSize=20,
        spaceAfter=6,
        textColor=colors.HexColor("#1a1a2e"),
    )
    heading_style = ParagraphStyle(
        "CustomHeading",
        parent=styles["Heading2"],
        fontSize=13,
        spaceBefore=14,
        spaceAfter=4,
        textColor=colors.HexColor("#16213e"),
    )
    subheading_style = ParagraphStyle(
        "SubHeading",
        parent=styles["Heading3"],
        fontSize=11,
        spaceBefore=10,
        spaceAfter=3,
        textColor=colors.HexColor("#0f3460"),
    )
    body_style = ParagraphStyle(
        "CustomBody",
        parent=styles["Normal"],
        fontSize=9,
        spaceAfter=3,
        leading=14,   # line height
    )

    story = []   # ReportLab builds PDFs from a list of "flowables" (elements)

    # --- HEADER ---
    story.append(Paragraph("Job Application Gap Analysis", title_style))
    story.append(Paragraph(
        f"Generated: {datetime.now().strftime('%B %d, %Y at %H:%M')} | "
        f"Jobs analysed: {len(jobs)}",
        body_style
    ))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#e0e0e0")))
    story.append(Spacer(1, 0.4*cm))

    # --- SECTION 1: SKILL HEATMAP ---
    # Count how many jobs list each skill as a gap.
    # Counter({'Docker': 8, 'Go': 5, 'Kubernetes': 4, ...})
    all_gaps = []
    for job in jobs:
        if job.missing_skills:
            all_gaps.extend(job.missing_skills)

    skill_counts = Counter(all_gaps).most_common(10)

    story.append(Paragraph("Skill Gap Heatmap", heading_style))
    story.append(Paragraph(
        "Skills most frequently required across all analysed jobs. "
        "High frequency = high ROI to learn.",
        body_style
    ))
    story.append(Spacer(1, 0.3*cm))

    if skill_counts:
        max_count = skill_counts[0][1]  # highest frequency for scaling bar widths

        heatmap_data = [["Skill", "Jobs Requiring It", "Frequency"]]
        for skill, count in skill_counts:
            # Build a text-based bar using block characters
            bar_length = int((count / max_count) * 20)
            bar = "█" * bar_length + "░" * (20 - bar_length)
            heatmap_data.append([skill, str(count), bar])

        heatmap_table = Table(
            heatmap_data,
            colWidths=[4*cm, 3.5*cm, 8*cm],
        )
        heatmap_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16213e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            ("FONTSIZE", (0, 1), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(heatmap_table)
    else:
        story.append(Paragraph("No skill gaps identified across analysed jobs.", body_style))

    story.append(Spacer(1, 0.6*cm))

    # --- SECTION 2: PRIORITY LEARNING LIST ---
    # Skills appearing in 2+ jobs, with course recommendations.
    # We deduplicate courses: if Docker appears in 8 jobs but they all
    # suggest the same course, only list it once.
    story.append(Paragraph("Priority Learning Recommendations", heading_style))
    story.append(Paragraph(
        "Courses for the skills that appear most frequently across your target jobs.",
        body_style
    ))
    story.append(Spacer(1, 0.3*cm))

    # Collect one course suggestion per skill (first one found)
    skill_to_course = {}
    for job in jobs:
        if not job.courses:
            continue
        for gap in job.courses:
            skill = gap.get("skill", "")
            if skill and skill not in skill_to_course:
                skill_to_course[skill] = gap

    # Sort by frequency descending
    priority_skills = [
        skill_to_course[skill]
        for skill, _ in skill_counts
        if skill in skill_to_course
    ]

    if priority_skills:
        course_data = [["Skill", "Importance", "Course", "Platform"]]
        for gap in priority_skills:
            course_data.append([
                gap.get("skill", ""),
                gap.get("importance", ""),
                gap.get("course", ""),
                gap.get("platform", ""),
            ])

        course_table = Table(
            course_data,
            colWidths=[3.5*cm, 2.5*cm, 7*cm, 3*cm],
        )
        course_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f3460")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            ("FONTSIZE", (0, 1), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4ff")]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("WORDWRAP", (2, 1), (2, -1), True),
        ]))
        story.append(course_table)
    
    story.append(Spacer(1, 0.6*cm))

    # --- SECTION 3: PER-JOB BREAKDOWN ---
    story.append(Paragraph("Per-Job Analysis", heading_style))

    for job in sorted(jobs, key=lambda j: j.score or 0, reverse=True):
        # Job header
        story.append(Paragraph(f"{job.title} — {job.company}", subheading_style))

        # Score bar
        score = job.score or 0
        score_bar_filled = int(score / 5)   # 100 / 5 = 20 blocks max
        score_bar = "█" * score_bar_filled + "░" * (20 - score_bar_filled)
        story.append(Paragraph(
            f"<font name='Courier'>{score_bar}</font>  {score:.0f}/100",
            body_style
        ))

        # Missing skills
        if job.missing_skills:
            skills_str = " · ".join(job.missing_skills)
            story.append(Paragraph(f"<b>Gaps:</b> {skills_str}", body_style))
        else:
            story.append(Paragraph("<b>Gaps:</b> None identified", body_style))

        story.append(Spacer(1, 0.2*cm))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e8e8e8")))

    # --- BUILD ---
    doc.build(story)
    return str(output_path)

    
"""Cambridge Writing assessment scales for B2 First / C1 Advanced / C2 Proficiency.

Pure data + a prompt builder for the writing grader (no I/O). Source: the Writing
"Assessment" pages of the 2026 handbooks for teachers (B2 First pp. 31-33, C1
Advanced pp. 33-35, C2 Proficiency pp. 31-33): four subscales — Content,
Communicative Achievement, Organisation, Language — each marked 0-5 in whole marks,
20 per task. "The subscale Content is common to all levels"; the other three use the
CEFR-level descriptors below (copied verbatim from the handbook table). Each exam's
scale anchors band 5 at its own level, band 3 one CEFR level below and band 1 two
below (B2: 5=B2, 3=B1, 1=A2; C1: 5=C1, 3=B2, 1=B1; C2: 5=C2, 3=C1, 1=B2); band 4
"shares features of Bands 3 and 5", band 2 "of Bands 1 and 3", 0 is below band 1.

A2 Key / B1 Preliminary / YLE writing keeps the existing holistic 0-5 prompt.
"""

from __future__ import annotations

CRITERIA: tuple[str, ...] = (
    "content",
    "communicative_achievement",
    "organisation",
    "language",
)
CRITERION_LABELS = {
    "content": "Content",
    "communicative_achievement": "Communicative Achievement",
    "organisation": "Organisation",
    "language": "Language",
}
CRITERION_MAX = 5
SCALE_MAX = CRITERION_MAX * len(CRITERIA)  # 20 per task

CONTENT_BANDS = {
    5: "All content is relevant to the task. Target reader is fully informed.",
    3: (
        "Minor irrelevances and/or omissions may be present. Target reader is on the "
        "whole informed."
    ),
    1: (
        "Irrelevances and misinterpretation of task may be present. Target reader is "
        "minimally informed."
    ),
    0: "Content is totally irrelevant. Target reader is not informed.",
}

# CEFR level -> (Communicative Achievement, Organisation, Language), verbatim.
CEFR_DESCRIPTORS: dict[str, tuple[str, str, str]] = {
    "C2": (
        "Demonstrates complete command of the conventions of the communicative task. "
        "Communicates complex ideas in an effective and convincing way, holding the "
        "target reader's attention with ease, fulfilling all communicative purposes.",
        "Text is organised impressively and coherently using a wide range of cohesive "
        "devices and organisational patterns with complete flexibility.",
        "Uses a wide range of vocabulary, including less common lexis, with fluency, "
        "precision, sophistication and style. Use of grammar is sophisticated, fully "
        "controlled and completely natural. Any inaccuracies occur only as slips.",
    ),
    "C1": (
        "Uses the conventions of the communicative task with sufficient flexibility to "
        "communicate complex ideas in an effective way, holding the target reader's "
        "attention with ease, fulfilling all communicative purposes.",
        "Text is a well-organised, coherent whole, using a variety of cohesive devices "
        "and organisational patterns with flexibility.",
        "Uses a range of vocabulary, including less common lexis, effectively and "
        "precisely. Uses a wide range of simple and complex grammatical forms with full "
        "control, flexibility and sophistication. Errors, if present, are related to "
        "less common words and structures, or occur as slips.",
    ),
    "B2": (
        "Uses the conventions of the communicative task effectively to hold the target "
        "reader's attention and communicate straightforward and complex ideas, as "
        "appropriate.",
        "Text is well organised and coherent, using a variety of cohesive devices and "
        "organisational patterns to generally good effect.",
        "Uses a range of vocabulary, including less common lexis, appropriately. Uses a "
        "range of simple and complex grammatical forms with control and flexibility. "
        "Occasional errors may be present but do not impede communication.",
    ),
    "B1": (
        "Uses the conventions of the communicative task to hold the target reader's "
        "attention and communicate straightforward ideas.",
        "Text is generally well organised and coherent, using a variety of linking words "
        "and cohesive devices.",
        "Uses a range of everyday vocabulary appropriately, with occasional inappropriate "
        "use of less common lexis. Uses a range of simple and some complex grammatical "
        "forms with a good degree of control. Errors do not impede communication.",
    ),
    "A2": (
        "Uses the conventions of the communicative task in generally appropriate ways to "
        "communicate straightforward ideas.",
        "Text is connected and coherent, using basic linking words and a limited number "
        "of cohesive devices.",
        "Uses everyday vocabulary generally appropriately, while occasionally overusing "
        "certain lexis. Uses simple grammatical forms with a good degree of control. "
        "While errors are noticeable, meaning can still be determined.",
    ),
}

# Exam level -> (exam name, CEFR of bands 5 / 3 / 1, word-length guidance).
EXAM_SCALES: dict[str, tuple[str, tuple[str, str, str], str]] = {
    "B2_FIRST": (
        "B2 First / B2 First for Schools",
        ("B2", "B1", "A2"),
        "140-190 words per task",
    ),
    "C1_ADVANCED": ("C1 Advanced", ("C1", "B2", "B1"), "220-260 words per task"),
    "C2_PROFICIENCY": (
        "C2 Proficiency",
        ("C2", "C1", "B2"),
        "Part 1 essay 240-280 words (summarises and evaluates the key ideas of TWO "
        "input texts); Part 2 280-320 words",
    ),
}


def has_scale(level: str | None) -> bool:
    """True when `level` is graded on the four-criteria 0-20 Cambridge scale."""
    return level in EXAM_SCALES


def scale_text(level: str) -> str:
    """The level's full assessment scale (bands 5..0 per subscale) as prompt text."""
    exam, (b5, b3, b1), length = EXAM_SCALES[level]
    lines = [
        f"{exam} WRITING ASSESSMENT SCALE (Cambridge handbook). Mark each subscale "
        "0-5 in WHOLE marks; the task total is out of 20.",
        "",
        "CONTENT (how well the task is fulfilled):",
    ]
    for band in (5, 3, 1):
        lines.append(f"  {band}: {CONTENT_BANDS[band]}")
    lines.append("  4: Performance shares features of Bands 3 and 5.")
    lines.append("  2: Performance shares features of Bands 1 and 3.")
    lines.append(f"  0: {CONTENT_BANDS[0]}")
    for idx, key in enumerate(("communicative_achievement", "organisation", "language")):
        lines.append("")
        lines.append(f"{CRITERION_LABELS[key].upper()}:")
        lines.append(f"  5 ({b5}): {CEFR_DESCRIPTORS[b5][idx]}")
        lines.append("  4: Performance shares features of Bands 3 and 5.")
        lines.append(f"  3 ({b3}): {CEFR_DESCRIPTORS[b3][idx]}")
        lines.append("  2: Performance shares features of Bands 1 and 3.")
        lines.append(f"  1 ({b1}): {CEFR_DESCRIPTORS[b1][idx]}")
        lines.append("  0: Performance below Band 1.")
    lines += [
        "",
        f"LENGTH GUIDANCE: {length}. Length is guidance, not a hard limit: a response "
        "that is too short may lack range or required information, one that is too "
        "long may contain irrelevant content — reflect that in the relevant subscales.",
        "Non-impeding errors (spelling, grammar, punctuation) that do not affect "
        "communication are not necessarily penalised; errors that interfere with or "
        "break down communication are treated more severely.",
    ]
    return "\n".join(lines)

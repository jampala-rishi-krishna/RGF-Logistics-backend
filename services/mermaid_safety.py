from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("agent_diagrams")

MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)\n```", re.IGNORECASE | re.DOTALL)
ALLOWED_STARTS = (
    "pie",
    "xychart-beta",
    "gantt",
    "flowchart",
    "stateDiagram-v2",
    "timeline",
)


@dataclass
class MermaidCheck:
    code: str
    diagram_type: str
    valid: bool
    reason: str = ""


def extract_mermaid_blocks(text: str) -> list[str]:
    return [match.group(1).strip() for match in MERMAID_BLOCK_RE.finditer(text or "")]


def diagram_type(code: str) -> str:
    first = next((line.strip() for line in (code or "").splitlines() if line.strip()), "")
    for prefix in ALLOWED_STARTS:
        if first.startswith(prefix):
            return prefix
    return first.split(maxsplit=1)[0] if first else "empty"


def validate_mermaid_code(code: str) -> MermaidCheck:
    cleaned = (code or "").strip()
    dtype = diagram_type(cleaned)
    if not cleaned:
        return MermaidCheck(code, dtype, False, "empty diagram")
    if dtype not in ALLOWED_STARTS:
        return MermaidCheck(code, dtype, False, "unsupported diagram type")
    if re.search(r"</?[A-Za-z][^>]*>", cleaned):
        return MermaidCheck(code, dtype, False, "html is not allowed")
    if "```" in cleaned:
        return MermaidCheck(code, dtype, False, "nested fence")

    lines = [line.rstrip() for line in cleaned.splitlines() if line.strip()]
    if dtype == "pie" and not any(":" in line for line in lines[1:]):
        return MermaidCheck(code, dtype, False, "pie chart has no slices")
    if dtype == "xychart-beta" and not any(line.strip().startswith(("bar ", "line ")) for line in lines):
        return MermaidCheck(code, dtype, False, "xychart has no series")
    if dtype == "gantt" and not any(":" in line for line in lines[1:]):
        return MermaidCheck(code, dtype, False, "gantt has no tasks")
    if dtype == "flowchart" and not any(arrow in cleaned for arrow in ("-->", "---", "-.->", "==>")):
        return MermaidCheck(code, dtype, False, "flowchart has no edges")
    if dtype == "stateDiagram-v2" and "-->" not in cleaned:
        return MermaidCheck(code, dtype, False, "state diagram has no transitions")
    if dtype == "timeline" and not any(":" in line for line in lines[1:]):
        return MermaidCheck(code, dtype, False, "timeline has no events")

    return MermaidCheck(code, dtype, True)


def validate_mermaid_blocks(text: str) -> list[MermaidCheck]:
    checks = [validate_mermaid_code(code) for code in extract_mermaid_blocks(text)]
    for check in checks:
        logger.info("diagram_type=%s valid=%s", check.diagram_type, check.valid)
    return checks


def remove_invalid_mermaid_blocks(text: str, checks: list[MermaidCheck]) -> str:
    invalid_codes = {check.code for check in checks if not check.valid}
    if not invalid_codes:
        return text

    def replace(match: re.Match[str]) -> str:
        code = match.group(1).strip()
        if code in invalid_codes:
            return "\nDiagram omitted because it could not be validated.\n"
        return match.group(0)

    return MERMAID_BLOCK_RE.sub(replace, text)

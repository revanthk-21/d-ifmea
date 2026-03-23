"""
routers/severity.py
POST /api/dfmea/severity-rate/bulk

LLM-based severity rating for DFMEA rows.
Uses the AIAG-VDA / Bosch severity rubric:

  S=10  Hazardous — safety risk, no warning
  S=9   Hazardous — safety risk, with warning
  S=8   Very High — primary function lost, vehicle inoperable
  S=7   High — primary function degraded, vehicle still operable
  S=6   Moderate — secondary function lost
  S=5   Moderate — secondary function degraded
  S=4   Low — noticeable deterioration (appearance, noise, vibration)
  S=3   Very Low — minor deterioration, most customers notice
  S=2   Minor — very minor effect, very discerning customers notice
  S=1   None — no discernible effect

The LLM is given the higher-level function and the failure effect, and
must output a structured JSON rating with justification.
"""

import json
import re
from fastapi import APIRouter
from pydantic import BaseModel
from llm_client import llm

router = APIRouter()

# ── Severity rubric sent verbatim to the LLM ─────────────────────────────────

SEVERITY_RUBRIC = """
Severity Rubric (AIAG-VDA aligned):

S=10  Hazardous effect without warning. Safety risk to vehicle occupants or others. Regulatory non-compliance.
S=9   Hazardous effect with warning. Safety risk to vehicle occupants or others. Regulatory non-compliance.
S=8   Very High. Primary vehicle or system function completely lost. Vehicle inoperable. Customer very dissatisfied.
S=7   High. Primary function degraded but still operational. Customer dissatisfied.
S=6   Moderate. Secondary function completely lost. Customer experiences discomfort.
S=5   Moderate. Secondary function degraded. Customer experiences some discomfort.
S=4   Low. Noticeable deterioration — appearance, NVH (noise, vibration, harshness). Most customers notice (>75%).
S=3   Very Low. Minor effect. About half of customers notice (50%).
S=2   Minor. Very minor effect. Very discerning customers notice (<25%).
S=1   None. No discernible effect on vehicle operation, customer, or quality.
""".strip()


# ── Request / Response models ─────────────────────────────────────────────────

class SeverityRow(BaseModel):
    row_id:          str
    higher_function: str
    failure_effect:  str


class BulkSeverityRequest(BaseModel):
    rows: list[SeverityRow]


# ── Core logic ────────────────────────────────────────────────────────────────

def _rate_severity(higher_function: str, failure_effect: str) -> dict:
    prompt = f"""TASK: Rate the severity of a DFMEA failure effect.

Higher-level function that is affected:
"{higher_function}"

Failure effect (what happens to that higher-level function):
"{failure_effect}"

{SEVERITY_RUBRIC}

INSTRUCTIONS:
1. Identify which rubric level best matches the failure effect on the higher-level function.
2. Consider the worst credible impact, not the average case.
3. Output ONLY valid JSON — no preamble, no markdown fences.

OUTPUT FORMAT:
{{
  "severity_rank": <integer 1-10>,
  "matched_rubric_bullet": "<copy the matching rubric line exactly>",
  "reason": "<one sentence explaining why this rank applies to this specific effect>"
}}
"""
    raw = llm.generate(prompt, max_tokens=300)

    # Strip any accidental markdown fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
        # Clamp to valid range
        result["severity_rank"] = max(1, min(10, int(result.get("severity_rank", 5))))
        return result
    except (json.JSONDecodeError, ValueError):
        # Fallback: try to extract just the number
        m = re.search(r'"severity_rank"\s*:\s*(\d+)', raw)
        rank = int(m.group(1)) if m else 5
        return {
            "severity_rank":        max(1, min(10, rank)),
            "matched_rubric_bullet": "Could not parse rubric match",
            "reason":                raw[:200],
        }


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/severity-rate/bulk")
def severity_rate_bulk(req: BulkSeverityRequest):
    """
    Rate severity for multiple DFMEA rows in one call.

    Response shape:
    {
      "results": [
        {
          "row_id":               "...",
          "severity_rank":        8,
          "matched_rubric_bullet": "S=8 Very High ...",
          "reason":               "..."
        },
        ...
      ]
    }
    """
    results = []
    for row in req.rows:
        if not row.failure_effect.strip() or not row.higher_function.strip():
            results.append({
                "row_id":               row.row_id,
                "severity_rank":        5,
                "matched_rubric_bullet": "No effect provided — defaulted to S=5",
                "reason":               "Empty failure effect or higher function",
            })
            continue

        rated = _rate_severity(row.higher_function, row.failure_effect)
        results.append({
            "row_id": row.row_id,
            **rated,
        })

    return {"results": results}

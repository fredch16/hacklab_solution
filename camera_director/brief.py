"""Turn a filming brief into bounded camera-space goals. The arm planner lives in orbit.py."""
import json
import math
from pathlib import Path

from claude_config import claude_api_key, load_env
from .orbit import MIN_FILM_ELEV


FRAMING = {"u": (0.15, 0.85), "v": (0.15, 0.85), "roll": (-18, 18)}
PARKED = {"pan": (-30, 30), **FRAMING}
FREE = {"theta": (-170, 170), "elev": (MIN_FILM_ELEV, 80), "dist": (0.13, 0.30), **FRAMING}


def validate(plan: dict, bounds: dict, actions: dict | None = None) -> dict:
    """Refuse a malformed plan as a whole before it reaches motion."""
    def number(value, lo, hi):
        return type(value) in (int, float) and math.isfinite(value) and lo <= value <= hi

    reason, shots = plan["reason"], plan["shots"]
    if not isinstance(reason, str) or not isinstance(shots, list) or len(shots) > 3:
        raise ValueError("invalid camera plan")
    action = plan.get("action", "custom")
    if action != "custom" and (not isinstance(action, str) or action not in (actions or {}) or shots
                               or not number(plan.get("hold"), 2, 6)):
        raise ValueError("the plan selected an unavailable command or mixed it with custom keyframes")
    for shot in shots:
        frames = shot["keyframes"]
        if (not isinstance(shot["name"], str) or not shot["name"].strip()
                or not number(shot["hold"], 2, 6) or not isinstance(frames, list) or not 2 <= len(frames) <= 6):
            raise ValueError("invalid take or hold duration")
        if any(not isinstance(frame, dict) or frame.keys() != bounds.keys()
               or not all(number(frame[k], *limits) for k, limits in bounds.items()) for frame in frames):
            raise ValueError("camera keyframe outside the allowed envelope")
    return plan


def plan(text: str, context: dict, computed: bool) -> dict:
    import anthropic

    load_env(Path(__file__).resolve().parents[1] / ".env")
    key = claude_api_key()
    if not key:
        raise ValueError("set CLAUDE_API_KEY to plan a filming brief")
    bounds = FREE if computed else PARKED
    actions = context.get("actions", {})
    lens = {"type": "object", "additionalProperties": False, "required": list(bounds),
            "properties": {k: {"type": "number"} for k in bounds}}
    shot = {"type": "object", "additionalProperties": False, "required": ["name", "hold", "keyframes"],
            "properties": {"name": {"type": "string"}, "hold": {"type": "number"},
                           "keyframes": {"type": "array", "items": lens}}}
    schema = {"type": "object", "additionalProperties": False, "required": ["action", "hold", "shots", "reason"],
              "properties": {"action": {"type": "string", "enum": ["custom", *actions]},
                             "hold": {"type": "number"}, "shots": {"type": "array", "items": shot},
                             "reason": {"type": "string"}}}
    coords = ("theta is subject-relative azimuth, 0 on the base side, positive from the right. Elev is degrees "
              "above the desk and dist is metres from the subject. Stay near the current camera coordinates "
              "and suggest small changes within the bounds, even if the current elevation is above them." if computed else
              "The shoulder and elbow stay parked. Pan is the relative sweep from the current camera, "
              "in degrees around the arm base, positive toward the robot's left. Each new take measures pan and "
              "roll from where the previous take ended. No dolly or crane is available.")
    client = anthropic.Anthropic(api_key=key, timeout=25, max_retries=0)
    response = client.messages.create(
        model="claude-opus-5-5", max_tokens=2200,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        system=("Direct a robot camera filming a selected subject. Interpret the brief as continuous camera "
                "constraints. Prefer an available existing action if it satisfies the brief, including a named "
                "view or equivalent viewing angle. Reuse a job only if its actual behavior matches the request. "
                "Return an empty shots list and a 2–6 second hold when choosing an existing action. If it does "
                "not fit, select custom and describe one to three takes with two to six nearby keyframes each. "
                "Each take has one deliberate motion followed by a 2–6 second hold. Use a single take when "
                "possible. Keep the subject fully visible. u/v is the subject center in image fractions, "
                "increasing right/down. Roll is degrees from level for a free camera, or from the current "
                "framing with the shoulder parked. " + coords + " Stay within the supplied bounds. "
                "If the local planner rejected a move, use its reason to try a smaller version of the same shot. "
                "For requests that cannot be filmed with these capabilities, return an empty shots list "
                "and explain the camera interface limitation without assuming which hardware is attached. "
                "You have no control tools."),
        messages=[{"role": "user", "content": json.dumps({"brief": text, "measured_context": context, "bounds": bounds})}],
    )
    if response.stop_reason != "end_turn":
        raise ValueError(f"Claude did not finish the shot plan ({response.stop_reason})")
    return validate(json.loads(next(b.text for b in response.content if b.type == "text")), bounds, actions)

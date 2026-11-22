"""Turn a filming brief into bounded camera-space goals. The arm planner lives in orbit.py."""
import json
import math
from pathlib import Path

from claude_config import claude_api_key, load_env
from .orbit import MIN_CAMERA_HEIGHT_M, MIN_FILM_ELEV


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
        if any("elev" in f and f["dist"] * math.sin(math.radians(f["elev"])) < MIN_CAMERA_HEIGHT_M for f in frames):
            raise ValueError(f"a keyframe would bring the camera below {MIN_CAMERA_HEIGHT_M * 100:.0f} cm above the table")
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
                "possible. Keep the subject fully visible and centred (u = v = 0.5) unless the brief asks for "
                "another composition. u/v is the subject center in image fractions, "
                "increasing right/down. Roll is degrees from level for a free camera, or from the current "
                "framing with the shoulder parked. " + coords + " Stay within the supplied bounds. "
                "If the local planner rejected a move, use its reason to try a smaller version of the same shot. "
                f"Safety: keep the camera well above the table at all times (dist x sin(elev) at least "
                f"{MIN_CAMERA_HEIGHT_M:.2f} m); never lower it toward the desk. For a closer shot make the subject "
                "larger in frame by coming in from above (shorter dist at the same or a higher elev, or tighter u/v "
                "framing), not by dropping the camera. Come only as close as keeps the whole subject comfortably "
                "inside the frame (at most about 60 percent of the frame's width or height) so it is never cut off "
                "or lost; if in doubt, stay a little wider. "
                "This arm (NormaCore ElRobot) overloads its shoulder servo when it reaches out with the elbow "
                "extended: prefer upright, retracted camera positions and small moves over long forward reaches. "
                "For a gimbal take, following the subject, or a cinematic show, choose that existing action. "
                "The arm is a 7-joint ElRobot holding the camera at its wrist. Single motions people ask for map onto "
                "these takes: nod = the subject's v swinging 0.35 / 0.65 a few times; shake / say no = pan swinging "
                "+-12 degrees a few times; look left / right = pan +-20 to 30; look up / down = v 0.7 / 0.3; tilt your "
                "head = roll +-15; sweep or scan = one slow pan across +-25; wave or say hi = the wave action. A gesture "
                "is one take with 4-6 keyframes and a short hold. Speed is set by the operator, not by you. "
                "For requests that cannot be filmed with these capabilities, return an empty shots list "
                "and explain the camera interface limitation without assuming which hardware is attached. "
                "You have no control tools."),
        messages=[{"role": "user", "content": json.dumps({"brief": text, "measured_context": context, "bounds": bounds})}],
    )
    if response.stop_reason != "end_turn":
        raise ValueError(f"Claude did not finish the shot plan ({response.stop_reason})")
    return validate(json.loads(next(b.text for b in response.content if b.type == "text")), bounds, actions)


# ---- gestures: motions of the arm itself (dance, nod, bow), nothing to do with a filmed subject ----
GESTURE = {"yaw": (-35, 35), "bob": (-10, 10), "pitch": (-25, 25), "roll": (-40, 40)}   # degrees from the view


def validate_gesture(plan: dict) -> dict:
    frames = plan.get("keyframes")
    if not isinstance(plan.get("reason"), str) or not isinstance(frames, list) or not 2 <= len(frames) <= 16:
        raise ValueError("invalid gesture plan")
    for f in frames:
        if not isinstance(f, dict) or f.keys() != GESTURE.keys() or not all(
                type(f[k]) in (int, float) and math.isfinite(f[k]) and lo <= f[k] <= hi for k, (lo, hi) in GESTURE.items()):
            raise ValueError("gesture keyframe outside the allowed range")
    return plan


def plan_gesture(text: str) -> dict:
    """A spoken gesture ('do a little dance', 'nod', 'bow') as keyframes of joint offsets from the current view."""
    import anthropic

    load_env(Path(__file__).resolve().parents[1] / ".env")
    key = claude_api_key()
    if not key:
        raise ValueError("set CLAUDE_API_KEY to plan a gesture")
    frame = {"type": "object", "additionalProperties": False, "required": list(GESTURE),
             "properties": {k: {"type": "number"} for k in GESTURE}}
    schema = {"type": "object", "additionalProperties": False, "required": ["keyframes", "reason"],
              "properties": {"keyframes": {"type": "array", "items": frame}, "reason": {"type": "string"}}}
    client = anthropic.Anthropic(api_key=key, timeout=25, max_retries=0)
    response = client.messages.create(
        model="claude-opus-5-5", max_tokens=2000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        system=("You choreograph a short expressive gesture for a 7-joint robot arm holding a camera, like a friendly "
                "character. Each keyframe gives offsets in degrees from where the arm stands now: yaw = the base "
                "turning left (+) / right (-); bob = the elbow pitch, a small up/down body bob; pitch = the wrist "
                "nodding up (+) / down (-); roll = the wrist tilting the head. Examples: nod = pitch 0, 15, -10, 15, 0; "
                "shake head = yaw 0, 15, -15, 15, -15, 0; bow = bob -8 with pitch -20, then back; a little dance = "
                "rhythmic alternating yaw +-15 with roll +-25 and a light bob; look around = slow yaw sweep with "
                "pitch up. Start and end at all zeros. 2-16 keyframes, small and playful, within the given ranges. "
                "You have no control tools; the robot checks and runs your plan."),
        messages=[{"role": "user", "content": json.dumps({"request": text, "ranges_deg": GESTURE})}],
    )
    if response.stop_reason != "end_turn":
        raise ValueError(f"Claude did not finish the gesture ({response.stop_reason})")
    return validate_gesture(json.loads(next(b.text for b in response.content if b.type == "text")))

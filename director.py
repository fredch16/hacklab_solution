"""M3: Claude as camera director. Type a request, Claude picks a saved view, the arm moves.

    uv run python director.py            # real arm via Station
    uv run python director.py --sim      # simulated arm (uses views.example.json if no views.json yet)

Every step is printed as heard -> decided -> moved and appended to events.jsonl
({"t", "kind", "text"}), which the overlay (E) and voice confirmation (D) read.
Needs ANTHROPIC_API_KEY.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

import anthropic

import motion
from motion import go_to_view, view_descriptions, view_names
from station_arm import run

MODEL = "claude-opus-5-5"
EVENTS_PATH = Path(__file__).parent / "events.jsonl"

SYSTEM = """You are the director for a camera mounted on a robot arm that films a person working at a desk.
The arm can only move between these saved viewpoints:
{views}

When the user asks to see something, call go_to_view with the viewpoint that best shows it.
- "zoom in" / "closer" means close; "everything" / "zoom out" means wide; "from above" means top.
- "go back" / "previous" means the viewpoint before the current one.
- If nothing fits well, pick the closest viewpoint and say what you chose.
- If the user is just chatting or asks something that needs no camera move, answer without calling the tool.
After the tool result, reply in one short sentence suitable for being spoken aloud."""


def log_event(kind: str, text: str):
    with EVENTS_PATH.open("a") as f:
        f.write(json.dumps({"t": time.time(), "kind": kind, "text": text}) + "\n")


def build_tools(names: list[str]) -> list[dict]:
    return [{
        "name": "go_to_view",
        "description": "Move the camera arm to one of the saved viewpoints. Takes 1-3 seconds.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": names, "description": "Name of the saved viewpoint"},
            },
            "required": ["view"],
            "additionalProperties": False,
        },
    }]


class Director:
    def __init__(self, arm, model: str = MODEL, effort: str = "low"):
        self.arm = arm
        self.model = model
        self.effort = effort
        self.client = anthropic.AsyncAnthropic()
        self.messages: list = []
        self.current: str | None = None
        names = view_names()
        if not names:
            raise RuntimeError("no saved views: run capture_view.py first (or use --sim)")
        descs = view_descriptions()
        self.system = SYSTEM.format(views="\n".join(f"- {n}: {descs.get(n) or n}" for n in names))
        self.tools = build_tools(names)

    async def handle(self, text: str) -> str:
        """One user request -> zero or more moves -> Claude's short reply."""
        print(f"  heard:   {text}")
        log_event("heard", text)
        state = f"[camera is currently at: {self.current or 'unknown'}]"
        self.messages.append({"role": "user", "content": f"{state}\n{text}"})
        while True:
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=4000,
                system=self.system,
                tools=self.tools,
                messages=self.messages,
                output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            self.messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "refusal":
                return "Sorry, I can't help with that."
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                return " ".join(b.text for b in response.content if b.type == "text").strip()
            results = []
            for tu in tool_uses:
                view = tu.input.get("view", "")
                print(f"  decided: go_to_view({view})")
                log_event("decided", view)
                result = await go_to_view(self.arm, view)
                if result["ok"]:
                    self.current = view
                    print(f"  moved:   {view} ({result.get('duration')}s)")
                    log_event("moved", view)
                else:
                    print(f"  FAILED:  {result['error']}")
                    log_event("error", f"{view}: {result['error']}")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(result),
                    "is_error": not result["ok"],
                })
            self.messages.append({"role": "user", "content": results})


async def main(args):
    if args.sim:
        from sim_arm import SimArm
        if not view_names():
            motion.VIEWS_PATH = Path(__file__).parent / "views.example.json"
        arm = await SimArm.connect()
    else:
        from station_arm import Arm
        arm = await Arm.connect(args.server, args.bus)
    director = Director(arm, model=args.model, effort=args.effort)
    print(f"Camera director ready ({'sim' if args.sim else 'bus ' + arm.bus_serial}, {args.model}). "
          f"Views: {', '.join(view_names())}. Ctrl-D to quit.\n")
    try:
        while True:
            text = (await asyncio.to_thread(input, "you> ")).strip()
            if not text:
                continue
            try:
                reply = await director.handle(text)
            except anthropic.APIConnectionError:
                reply = "(network error talking to Claude)"
            except anthropic.APIStatusError as e:
                reply = f"(Claude API error {e.status_code}: {e.message})"
            print(f"  claude:  {reply}\n")
            log_event("reply", reply)
    finally:
        arm.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sim", action="store_true", help="simulated arm, no hardware")
    p.add_argument("--model", default=MODEL)
    p.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    p.add_argument("--server", default="localhost")
    p.add_argument("--bus", default="auto")
    run(main(p.parse_args()))

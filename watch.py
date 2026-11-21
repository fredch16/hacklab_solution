"""Keep one object in the camera's picture: look at it about once a second and, when it stays
covered (a hand, a tool), move the camera to another saved view.

Runs as one thread inside voice_bridge.py and is, like the bridge, only a client of the camera-arm
MCP server: it reads get_status and get_camera_image and moves with go_to_view, so every move is a
normal move between saved views with all of the motion layer's checks. It never sends joint
values or free targets.

    "keep the pcb in view"   -> Watcher.start("pcb")
    "look around for the pcb"-> Watcher.start("pcb", survey=True)   visits each checked view once first
    "stop following", "stop" -> Watcher.stop()

How it decides (class Settings):
  * one look = one camera image judged by Claude (class Judge): is the object in the picture, and
    how much of it is covered? -> visible 0..1
  * "covered" means: visible below BLOCKED_BELOW in NEED of the last WINDOW looks. One hand passing
    through does nothing; a hand that stays triggers a move after a few seconds.
  * the next view is the best remembered one (how large and central the object was when it was last
    clear there), then views not looked from yet. The view just left is avoided for AVOID_S.
  * before it moves it announces the move for ANNOUNCE_S (status "announcing"; cancel() keeps the
    camera where it is), and after a move it stays for at least DWELL_S.

It moves the arm on its own, next to people, so:
  * every move carries the STOP generation seen at the look that caused it (go_to_view's
    since_generation). A STOP from anywhere (voice, the red button, the /control page, Claude)
    after that look makes the control server refuse the move; and following then turns itself off.
  * it refuses to start against an MCP server that does not report that generation.
  * a move of its own that does not arrive turns following off. It never retries a failed move.
  * while the arm is blocked (limp, unknown pose) or moved by someone else it only waits.

Standard library, the MCP SDK and, for Judge, the Anthropic SDK.
"""
import asyncio
import base64
import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass

from mcp import Client

log = logging.getLogger("watch")

JUDGE_MODEL = "claude-haiku-5-5"     # about a second per picture; the loop's pace depends on it
JUDGE_TIMEOUT_S = 8.0

JUDGE_SYSTEM = """\
You look at one picture from a camera that a robot arm holds above a desk where a person works on \
electronics. A program asks you about ONE object, named in the request, and uses your answer to \
decide whether the camera should move to another position. Reply only with the JSON object the \
schema asks for.

- in_frame: "yes" when the whole object lies within the picture's edges, also when something \
covers it; "partly" when an edge of the picture cuts it off; "no" when it is not in the picture \
or you cannot tell where it is.
- covered: how much of the object is hidden behind something in front of it (a hand, an arm, a \
tool, a cable): "none" (practically all of it can be seen), "some" (up to about half is hidden), \
"most" (more than half is hidden), "all" (nothing of it can be seen, but it is evident where it \
lies, for example under a hand). Motion blur over the object counts as covered.
- by: what covers it: "hand" (hands, arms, fingers), "tool", "robot", "other", or "nothing". The \
black gripper jaws that show at the bottom edge of every picture belong to the camera's own arm: \
that is "robot".
- cx, cy, w, h: a box around the whole object, including the covered part, as fractions of the \
picture (cx, cy = centre, 0,0 = top left corner; w, h = width and height). All 0 when in_frame is "no".

Judge only what this picture shows. If several things could be the object, take the one that fits \
the name best. When in doubt between two levels of "covered", choose the more covered one: the \
camera moving once too often costs less than filming a hand."""

VISIBLE = {"none": 1.0, "some": 0.65, "most": 0.25, "all": 0.0}
JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["in_frame", "covered", "by", "cx", "cy", "w", "h"],
    "properties": {
        "in_frame": {"type": "string", "enum": ["yes", "partly", "no"]},
        "covered": {"type": "string", "enum": list(VISIBLE)},
        "by": {"type": "string", "enum": ["hand", "tool", "robot", "other", "nothing"]},
        "cx": {"type": "number"}, "cy": {"type": "number"}, "w": {"type": "number"}, "h": {"type": "number"},
    },
}


def look_from_answer(out: dict) -> dict | None:
    """The judge's JSON as a look: {"visible": 0..1, "in_frame", "covered", "by", "bbox" or None}.
    None when the answer is not usable."""
    try:
        in_frame, covered, by = out["in_frame"], out["covered"], out["by"]
        nums = [float(out[k]) for k in ("cx", "cy", "w", "h")]
    except (KeyError, TypeError, ValueError):
        return None
    if in_frame not in ("yes", "partly", "no") or covered not in VISIBLE or not all(math.isfinite(n) for n in nums):
        return None
    if in_frame == "no":
        return {"visible": 0.0, "in_frame": "no", "covered": covered, "by": by, "bbox": None}
    cx, cy, w, h = (min(1.0, max(0.0, n)) for n in nums)
    bbox = {"cx": cx, "cy": cy, "w": w, "h": h} if w > 0.005 and h > 0.005 else None
    visible = VISIBLE[covered] * (0.6 if in_frame == "partly" else 1.0)
    return {"visible": round(visible, 2), "in_frame": in_frame, "covered": covered, "by": by, "bbox": bbox}


class Judge:
    """One picture -> how visible the named object is, with Claude. `client` is for tests."""

    def __init__(self, model: str = JUDGE_MODEL, timeout_s: float = JUDGE_TIMEOUT_S, client=None):
        import anthropic
        self._errors = anthropic
        self.client = client or anthropic.Anthropic(timeout=timeout_s, max_retries=0)
        self.model = model

    def look(self, jpeg: bytes, label: str) -> dict | None:
        fast = "haiku" in self.model
        t0 = time.monotonic()
        try:
            res = self.client.messages.create(
                model=self.model, max_tokens=300 if fast else 4000, system=JUDGE_SYSTEM,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.standard_b64encode(jpeg).decode()}},
                    {"type": "text", "text": f"The object: {label}"}]}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
                **({"thinking": {"type": "disabled"}} if fast else {}))
        except self._errors.APITimeoutError:
            log.warning("judge: no answer within the time limit")
            return None
        except self._errors.APIConnectionError as e:
            log.warning("judge: the Anthropic API is not reachable (%s)", e)
            return None
        except self._errors.AuthenticationError:
            log.warning("judge: the API key was rejected")
            return None
        except self._errors.RateLimitError:
            log.warning("judge: rate limited")
            return None
        except self._errors.APIStatusError as e:
            log.warning("judge: API error %s: %s", e.status_code, e.message)
            return None
        if res.stop_reason != "end_turn":
            log.warning("judge: stopped with %s", res.stop_reason)
            return None
        raw = next((b.text for b in res.content if b.type == "text"), "")
        try:
            look = look_from_answer(json.loads(raw))
        except ValueError:
            look = None
        if look is None:
            log.warning("judge: unusable answer %r", raw[:200])
            return None
        look["took"] = round(time.monotonic() - t0, 2)
        return look


# ---------- the rules, as plain functions ----------
@dataclass
class Settings:
    period_s: float = 1.0          # at most one look per this many seconds
    window: int = 4                # looks that count
    need: int = 3                  # ...of which this many must be covered before a move
    blocked_below: float = 0.4     # a look is "covered" below this visibility
    clear_above: float = 0.6       # a look this visible says how good the view is when nothing is in the way
    dwell_s: float = 8.0           # after arriving, stay at least this long
    announce_s: float = 2.5        # a move is announced this long before it starts
    avoid_s: float = 45.0          # a view left because it was covered is not chosen again this soon
    stay_s: float = 20.0           # cancel(): no automatic move for this long
    arrive_timeout_s: float = 120.0
    skip_views: tuple = ("home", "transit")   # never chosen: not camera positions


def covered(history: list, s: Settings) -> bool:
    """history: visibilities of the looks since the camera arrived, oldest first."""
    recent = history[-s.window:]
    return len(recent) >= s.need and sum(1 for v in recent if v < s.blocked_below) >= s.need


def quality(look: dict) -> float:
    """How good a view is for the object when nothing is in the way: large and central is better."""
    b = look.get("bbox")
    if not b:
        return 0.0
    size = min(1.0, (b["w"] * b["h"]) / 0.25)
    off = min(1.0, math.hypot(b["cx"] - 0.5, b["cy"] - 0.5) / 0.5)
    part = 0.6 if look.get("in_frame") == "partly" else 1.0
    return round((0.3 + 0.7 * size) * (1 - 0.5 * off) * part, 3)


def choose(memory: dict, views: list[str], current: str | None, now: float, s: Settings) -> str | None:
    """The view to try next: best remembered quality first, then views never looked from, then views
    where the object was not found. Never the present one, a skipped one, or one left recently."""
    def rank(v):
        m = memory.get(v) or {}
        q = m.get("quality")
        return (0, -q) if q else (1, 0) if q is None else (2, 0)
    ok = [v for v in views if v != current and v not in s.skip_views
          and (memory.get(v) or {}).get("avoid_until", 0) <= now]
    return min(ok, key=lambda v: (rank(v), views.index(v))) if ok else None


def slug(label: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", label.lower()).strip("_")[:40] or "object"


class Refusal(Exception):
    """An MCP tool said no; .detail is its reason in plain words."""
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _result(res) -> dict:
    if res.is_error:
        text = res.content[0].text if res.content else "tool error"
        try:
            text = json.loads(text).get("detail") or text
        except (ValueError, AttributeError):
            pass
        raise Refusal(str(text))
    return res.structured_content or {}


class Watcher:
    """start()/stop()/cancel()/status() may be called from any thread."""

    def __init__(self, target, judge, settings: Settings | None = None):
        """target: the MCP server (URL, or an MCPServer in tests). judge: .look(jpeg, label)."""
        self.target, self.judge, self.s = target, judge, settings or Settings()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._quit = False
        self._reset()
        self.events: list[dict] = []        # the last things that happened, newest last

    def _reset(self):
        self.active = False
        self.label = ""
        self.phase = "off"                  # off | looking | announcing | moving | surveying | waiting
        self.message = ""
        self.view: str | None = None
        self.look: dict | None = None
        self.history: list[float] = []      # since arriving at this view
        self.memory: dict[str, dict] = {}   # view -> {quality, visible, at, avoid_until}
        self.next_view: str | None = None
        self.move_at = 0.0                  # when the announced move starts
        self.moves = 0
        self._gen: int | None = None        # STOP generation following started with
        self._my_target: str | None = None  # a move of ours that is under way
        self._dwell_until = 0.0
        self._stay_until = 0.0
        self._cancelled = False
        self._survey: list[str] | None = None
        self._epoch = getattr(self, "_epoch", 0) + 1

    # ----- called from the bridge -----
    def start(self, label: str, survey: bool = False) -> str:
        label = re.sub(r"\s+", " ", label).strip()[:60]
        if not label:
            return "Say what to keep in view, for example “keep the PCB in view”"
        with self._lock:
            self._reset()
            self.active, self.label, self.phase = True, label, "looking"
            self._survey = [] if survey else None
            self.message = f"Looking for the {label}"
            self._note(f"following the {label}" + (" (looking around first)" if survey else ""))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True, name="watch")
                self._thread.start()
        self._wake.set()
        return f"Looking around for the {label}" if survey else f"Keeping the {label} in view"

    def stop(self, why: str = "Following is off") -> bool:
        """Turn following off. After this returns no new move is started. True if it was on."""
        with self._lock:
            was = self.active
            if was:
                self._note(why)
            label = self.label
            self._reset()
            self.message = why if was else ""
            self.label = label if was else ""
        self._wake.set()
        return was

    def cancel(self) -> bool:
        """Keep the camera where it is: drops an announced move and holds off for stay_s."""
        with self._lock:
            if not self.active:
                return False
            self._cancelled = True
            self._stay_until = time.monotonic() + self.s.stay_s
            self.next_view, self.history = None, []
            if self.phase == "announcing":
                self.phase = "looking"
            self.message = f"Staying here for {round(self.s.stay_s)} s"
            self._note("staying here")
        self._wake.set()
        return True

    def close(self):
        self._quit = True
        self.stop()

    def status(self) -> dict:
        with self._lock:
            now = time.monotonic()
            recent = self.history[-self.s.window:]
            return {
                "available": True, "active": self.active, "object": self.label, "phase": self.phase,
                "message": self.message, "view": self.view,
                "visible": self.look["visible"] if self.look else None,
                "covered_by": self.look["by"] if self.look and self.look["visible"] < self.s.blocked_below else None,
                "covered_looks": sum(1 for v in recent if v < self.s.blocked_below), "need": self.s.need,
                "next_view": self.next_view,
                "moves_in_s": round(max(0.0, self.move_at - now), 1) if self.phase == "announcing" else None,
                "moves": self.moves, "events": self.events[-8:],
            }

    def _note(self, text: str):
        log.info("%s", text)
        self.events.append({"at": round(time.time(), 1), "text": text})
        del self.events[:-30]

    # ----- the loop (its own thread) -----
    async def _main(self):
        while not self._quit:
            if not self.active:
                self._wake.clear()
                await asyncio.to_thread(self._wake.wait, 1.0)
                continue
            t0 = time.monotonic()
            epoch = self._epoch
            try:
                await self._tick(epoch)
            except Refusal as e:
                self._say(epoch, "waiting", e.detail)
            except Exception as e:     # MCP server down, protocol error: wait, never guess
                log.warning("watch tick failed: %r", e)
                self._say(epoch, "waiting", "The camera-arm MCP server is not reachable")
            await asyncio.sleep(max(0.05, self.s.period_s - (time.monotonic() - t0)))

    def _say(self, epoch: int, phase: str, message: str):
        with self._lock:
            if epoch == self._epoch and self.active:
                self.phase, self.message = phase, message

    def _off(self, epoch: int, why: str):
        with self._lock:
            if epoch == self._epoch and self.active:
                self.stop(why)

    async def _tick(self, epoch: int):
        s = self.s
        async with Client(self.target, read_timeout_seconds=10) as mcp:
            st = _result(await mcp.call_tool("get_status", {}))
            gen = st.get("generation")
            if not isinstance(gen, int):
                return self._off(epoch, "Following needs the current MCP server: restart mcp_server.py")
            with self._lock:
                if epoch != self._epoch:
                    return
                if self._gen is None:
                    self._gen = gen
            if gen != self._gen:
                return self._off(epoch, "STOP was pressed: following is off")

            if st.get("moving"):
                mine = st.get("target_view") == self._my_target
                return self._say(epoch, "moving", f"Moving to {st.get('target_view')}" if mine
                                 else f"Waiting: the arm is moving to {st.get('target_view')}")
            now = time.monotonic()
            if self._my_target:              # our move has ended: did it arrive?
                target, self._my_target = self._my_target, None
                last = st.get("last_result") or {}
                if st.get("current_view") != target:
                    why = last.get("error") or "it did not arrive"
                    return self._off(epoch, f"The move to {target} did not finish ({why}): following is off")
                self._arrived(epoch, target, now)
            if st.get("blockers") or st.get("can_move") is False:
                why = (st.get("blockers") or ["the arm cannot move right now"])[0]
                return self._say(epoch, "waiting", f"Waiting: {why}")
            view = st.get("current_view")
            if view != self.view:            # someone chose another view: carry on from there
                self._arrived(epoch, view, now)

            if self._survey is not None and not self._survey and not self.memory:
                checked = [v["name"] for v in _result(await mcp.call_tool("list_views", {})).get("views", [])
                           if v.get("checked") and v["name"] not in s.skip_views]
                with self._lock:
                    if epoch == self._epoch:
                        self._survey = [v for v in checked if v != view]

            image = await mcp.call_tool("get_camera_image", {})
            if image.is_error:
                _result(image)
            jpeg = base64.b64decode(image.content[0].data)
            meta = json.loads(image.content[1].text)
            look = await asyncio.to_thread(self.judge.look, jpeg, self.label)
            if look is None:
                return self._say(epoch, self.phase if self.phase != "off" else "looking",
                                 "Could not judge the picture just now")
            now = time.monotonic()
            with self._lock:
                if epoch != self._epoch:
                    return
                self.look = look
                self.history.append(look["visible"])
                del self.history[:-20]
                if view:
                    m = self.memory.setdefault(view, {"quality": None, "avoid_until": 0})
                    m.update(visible=look["visible"], at=now)
                    if look["visible"] >= s.clear_above:
                        m["quality"] = quality(look)
                    elif look["in_frame"] == "no" and m["quality"] is None and len(self.history) >= s.need \
                            and all(v == 0 for v in self.history[-s.need:]):
                        m["quality"] = 0.0          # looked several times, never found it here
            if view and look["bbox"]:
                await self._record(mcp, meta.get("still"), look)

            target = self._decide(epoch, view, now)
            if target is None:
                return
            if not await self._announce(epoch, target):
                return
            await self._go(epoch, mcp, view, target, gen)

    def _arrived(self, epoch: int, view: str | None, now: float):
        with self._lock:
            if epoch != self._epoch:
                return
            self.view, self.history, self.look = view, [], None
            self._dwell_until = now + (0 if self._survey else self.s.dwell_s)

    async def _record(self, mcp, still, look: dict):
        """Keep the scene's sightings fresh for best_view and Claude. Failing here changes nothing."""
        try:
            await mcp.call_tool("update_scene", {"objects": [{
                "id": slug(self.label), "label": self.label,
                "seen": [{"still": still, "bbox": look["bbox"], "visible": look["visible"]}]}]})
        except Exception as e:
            log.debug("could not record the sighting: %r", e)

    def _decide(self, epoch: int, view: str | None, now: float) -> str | None:
        """After a look: the view to move to, or None to stay. Sets phase and message."""
        s = self.s
        with self._lock:
            if epoch != self._epoch or not self.active:
                return None
            look = self.look
            seen = "in view" if look["visible"] >= s.clear_above else \
                "not in the picture" if look["in_frame"] == "no" else f"covered ({look['by']})"
            if self._survey is not None:
                if self._survey:
                    self.phase, self.message = "surveying", f"Looked from {view}: {seen}"
                    return self._survey[0]
                self._survey = None          # all views seen: go to the best one, then watch
                best = max((v for v in self.memory if self.memory[v].get("quality")),
                           key=lambda v: self.memory[v]["quality"], default=None)
                self._note("looked around: " + ", ".join(
                    f"{v} {self.memory[v]['quality'] if self.memory[v].get('quality') is not None else '?'}"
                    for v in self.memory))
                if best and best != view:
                    self.phase, self.message = "surveying", f"Best view of the {self.label}: {best}"
                    return best
            self.phase = "looking"
            where = f"At {view}" if view else "Between views"
            if now < self._stay_until:
                self.message = f"{where}: {seen}. Staying here"
                return None
            if now < self._dwell_until or not covered(self.history, s):
                self.message = f"{where}: the {self.label} is {seen}"
                return None
            views = self._views
            target = choose(self.memory, views, view, now, s)
            if target is None:
                self.message = f"{where}: the {self.label} is {seen}, and there is no other view to try yet"
                return None
            return target

    async def _announce(self, epoch: int, target: str) -> bool:
        """Say where the camera is about to go and give announce_s to object. True = go."""
        with self._lock:
            if epoch != self._epoch or not self.active:
                return False
            survey = self._survey is not None or self.phase == "surveying"
            wait = 0.0 if survey else self.s.announce_s
            self._cancelled = False
            self.next_view, self.move_at = target, time.monotonic() + wait
            if not survey:
                self.phase = "announcing"
                self.message = f"The {self.label} is covered: moving to {target}"
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            await asyncio.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            if epoch != self._epoch or self._cancelled:
                return False
        return True

    async def _go(self, epoch: int, mcp, view: str | None, target: str, gen: int):
        # Under the lock: stop() and cancel() either come before this (and nothing is sent) or find the
        # move already started. since_generation makes the control server refuse it after any STOP.
        with self._lock:
            if epoch != self._epoch or not self.active or self._cancelled:
                return
            try:
                r = _result(await mcp.call_tool("go_to_view", {"view": target, "since_generation": gen}))
            except Refusal as e:
                self.next_view = None
                if "STOP" in e.detail or "stale" in e.detail.lower() or "reload" in e.detail.lower():
                    return self.stop("STOP was pressed: following is off")
                self.phase, self.message = "waiting", f"The move to {target} was refused: {e.detail}"
                self._stay_until = time.monotonic() + self.s.dwell_s
                return
            now = time.monotonic()
            if self._survey:
                self._survey = [v for v in self._survey if v != target]
            elif view:
                self.memory.setdefault(view, {"quality": None})["avoid_until"] = now + self.s.avoid_s
            self.next_view = None
            if r.get("started"):
                self._my_target, self.moves = target, self.moves + 1
                self.phase, self.message = "moving", f"Moving to {target}"
                self._note(f"moving {view or '?'} -> {target}")
            else:                             # already there
                self._arrived(epoch, target, now)

    @property
    def _views(self) -> list[str]:
        return self.__dict__.get("_view_list") or []

    async def refresh_views(self):
        """The checked views, in the server's order. Called before the first decision and now and then."""
        async with Client(self.target, read_timeout_seconds=10) as mcp:
            views = _result(await mcp.call_tool("list_views", {})).get("views", [])
        self.__dict__["_view_list"] = [v["name"] for v in views if v.get("checked")]

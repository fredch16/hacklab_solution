import { armResult } from "./voice.js";
// Transport to the PC-side arm command service (voice_bridge.py in the hackathon repo). The service,
// not this bundle, holds the MCP connection and tool policy. Default: the service on the PC running
// this browser. `?arm=<url>` overrides it (remembered per browser), `?arm=off` turns forwarding off.
const KEY = "lv.armBridge";
export const DEFAULT_ARM_BRIDGE = "http://127.0.0.1:8210";
export function armBridgeUrl(search = location.search) {
    let v = null;
    try {
        const q = new URLSearchParams(search).get("arm");
        if (q !== null)
            localStorage.setItem(KEY, q);
        v = localStorage.getItem(KEY);
    }
    catch { /* storage blocked: use the default */ }
    if (v === "off")
        return null;
    return v && /^https?:\/\/[^\s/]+$/.test(v) ? v : DEFAULT_ARM_BRIDGE;
}
const inflight = new Set();
/** POST one eligible final; resolves with the service's outcome or a transport failure. Never retried. */
export async function forwardToArm(base, req, timeoutMs = 15000) {
    const key = `${req.session_id}/${req.utterance_id}`;
    if (inflight.has(key))
        return null;
    inflight.add(key);
    const ctl = new AbortController();
    const timer = window.setTimeout(() => ctl.abort(), timeoutMs);
    try {
        const r = await fetch(`${base}/api/v1/voice-command`, {
            method: "POST", mode: "cors", credentials: "omit", cache: "no-store", signal: ctl.signal,
            headers: { "content-type": "application/json" }, body: JSON.stringify(req),
        });
        return armResult(await r.json().catch(() => null));
    }
    catch {
        return armResult(null);
    }
    finally {
        clearTimeout(timer);
        inflight.delete(key);
    }
}

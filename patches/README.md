Patches to NormaCore's station-viewer (v0.1.0-beta.10, `software/station/clients/station-viewer`)
so the web UI matches our desk: "Swap sides" for the two arm cards and "Rotate view" for the 3D view.

Apply to a copy of station-viewer, then `npm ci && npx tsc -b && npx vite build &&
npx vite preview --host 127.0.0.1 --port 5173`. It proxies `/api` to the running Station on :8889.
New file `src/st3215/view-rotation.ts` is included in the swap diff.

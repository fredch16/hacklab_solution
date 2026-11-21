Patch to NormaCore's station-viewer (v0.1.0-beta.10, `software/station/clients/station-viewer`)
so the web UI matches our desk: "Swap sides" for the two arm cards, and "Rotate view", which turns
the 3D arm models about their base in 90° steps (default 90° anticlockwise).

    cp -R <norma-core>/software/station/clients/station-viewer viewer && cd viewer
    patch -p1 -d src < ../patches/station-viewer.diff   # or apply by hand; paths are src/...
    npm ci && npx tsc -b && npx vite build
    npx vite preview --host 127.0.0.1 --port 5173 --strictPort

The preview proxies `/api` to the running Station on :8889. Hard-reload (Cmd+Shift+R) after rebuilding.

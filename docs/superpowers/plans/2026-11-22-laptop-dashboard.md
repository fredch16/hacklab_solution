# Laptop dashboard implementation plan

1. Rebuild the existing TypeScript interface as laptop assets. Verify phone pairing and event relay with a pinned TLS test phone and keep the video peer direct.
2. Add the Station JPEG feed and a tab-owned robot recording. Verify finalization, download, hidden tab, disconnect, stale frame, and an unrelated tab's refusal. Keep the phone recorder native.
3. Route explicit speech and MCP/Claude recording actions to the two independent recorders. Verify phone is the unqualified default, a failed acoustic check cannot trigger a phone action, and every robot start requires a live visible tab.
4. Verify simulated integrations and document exact startup, troubleshooting, and owner Pixel/laptop checks before the APK cutover. No supervised real-arm movement is part of agent verification.

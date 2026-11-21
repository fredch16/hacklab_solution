import qrcode from "./qrcode.js";
/** Module matrix for `text` (byte mode, ECC M, smallest version). */
export function qrMatrix(text) {
    const q = qrcode(0, "M");
    q.addData(text, "Byte");
    q.make();
    const n = q.getModuleCount();
    return Array.from({ length: n }, (_, r) => Array.from({ length: n }, (_, c) => q.isDark(r, c)));
}
/** Dark-on-white canvas, 4-module quiet zone, at least `minPx` wide. Sized via attributes (CSP-safe). */
export function qrCanvas(text, minPx = 240) {
    const m = qrMatrix(text);
    const total = m.length + 8;
    const cell = Math.ceil(minPx / total);
    const cv = document.createElement("canvas");
    cv.width = cv.height = total * cell;
    const g = cv.getContext("2d");
    if (g) {
        g.fillStyle = "#fff";
        g.fillRect(0, 0, cv.width, cv.height);
        g.fillStyle = "#000";
        m.forEach((row, r) => row.forEach((d, c) => { if (d)
            g.fillRect((c + 4) * cell, (r + 4) * cell, cell, cell); }));
    }
    return cv;
}

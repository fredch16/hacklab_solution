// Minimal typings for vendor/qrcode.js (qrcode-generator 2.0.4, MIT, Kazuhiko Arase).
interface QRCode { addData(data: string, mode?: "Numeric" | "Alphanumeric" | "Byte"): void; make(): void; getModuleCount(): number; isDark(row: number, col: number): boolean }
declare const qrcode: (typeNumber: number, errorCorrectionLevel: "L" | "M" | "Q" | "H") => QRCode;
export default qrcode;

import { copyFile, mkdir } from "node:fs/promises";

const files = [
	"index.js",
	"index.d.ts",
	"policy.js",
	"transport.js",
	"evidence.js",
	"actions.js",
];
await mkdir(new URL("../dist/", import.meta.url), { recursive: true });
for (const file of files) {
	await copyFile(
		new URL(`../src/${file}`, import.meta.url),
		new URL(`../dist/${file}`, import.meta.url),
	);
}

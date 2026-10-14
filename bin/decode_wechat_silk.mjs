#!/usr/bin/env node

import { open, readFile } from "node:fs/promises";
import { lstat } from "node:fs/promises";
import path from "node:path";

globalThis.fetch = async () => {
  throw new Error("network_disabled");
};

const [sourceArg, destinationArg, sampleRateArg = "24000"] = process.argv.slice(2);
if (sourceArg === "--self-test") {
  try {
    const { decode, encode, isSilk } = await import("../vendor/wechat-media/node_modules/silk-wasm/lib/index.mjs");
    const pcm = new Int16Array(2400);
    for (let index = 0; index < pcm.length; index += 1) {
      pcm[index] = Math.round(Math.sin(index / 12) * 4000);
    }
    const encoded = await encode(new Uint8Array(pcm.buffer), 24000);
    const decoded = await decode(encoded.data, 24000);
    if (!isSilk(encoded.data) || decoded.data.byteLength === 0 || decoded.data.byteLength % 2 !== 0) {
      throw new Error("self_test_failed");
    }
    process.stdout.write(JSON.stringify({ verdict: "pass", checks: 3 }) + "\n");
    process.exit(0);
  } catch {
    process.stderr.write("self_test_failed\n");
    process.exit(2);
  }
}
if (!sourceArg || !destinationArg) {
  process.stderr.write("usage_error\n");
  process.exit(2);
}

const source = path.resolve(sourceArg);
const destination = path.resolve(destinationArg);
const sampleRate = Number.parseInt(sampleRateArg, 10);
if (![8000, 12000, 16000, 24000, 32000, 44100, 48000].includes(sampleRate)) {
  process.stderr.write("sample_rate_invalid\n");
  process.exit(2);
}

try {
  const info = await lstat(source);
  if (!info.isFile() || info.isSymbolicLink() || info.size < 7 || info.size > 128 * 1024 * 1024) {
    throw new Error("source_invalid");
  }
  const input = await readFile(source);
  const { decode, isSilk } = await import("../vendor/wechat-media/node_modules/silk-wasm/lib/index.mjs");
  if (!isSilk(input)) {
    throw new Error("not_silk");
  }
  const result = await decode(input, sampleRate);
  if (!result.data || result.data.byteLength === 0 || result.data.byteLength % 2 !== 0 || result.data.byteLength > 2 * 1024 * 1024 * 1024) {
    throw new Error("decode_invalid");
  }
  const handle = await open(destination, "wx", 0o600);
  try {
    await handle.writeFile(result.data);
    await handle.sync();
  } finally {
    await handle.close();
  }
  process.stdout.write(JSON.stringify({ verdict: "pass", duration_ms: result.duration, sample_rate: sampleRate }) + "\n");
} catch (error) {
  const code = ["source_invalid", "not_silk", "decode_invalid"].includes(error?.message)
    ? error.message
    : "silk_decode_failed";
  process.stderr.write(code + "\n");
  process.exit(2);
}

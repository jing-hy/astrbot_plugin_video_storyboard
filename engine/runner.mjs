#!/usr/bin/env node
// runner.mjs — AstrBot 插件用的引擎入口。
// 入参：一个 JSON 文件路径（argv[2]），出参：stdout 一个 JSON。
// 与 DSH 插件共用同一份 pipeline.js，只做「入参/出参」的翻译：
//   PNG 写进 outDir（不走 stdout 大 buffer），清单文本原样带回。
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

import {
  DETAIL_CHARS, formatManifest, formatProbe, formatRemoteProbe, runStoryboard,
} from './pipeline.js';
import { probeVideo } from './probe.js';
import { formatMatches, matchSegments, recallSegments } from './locate.js';
import { lookupCell } from './cells.js';
import { isHttpUrl, probeUrl, resolveInput } from './fetch.js';

function fail(error) {
  process.stdout.write(JSON.stringify({ ok: false, error: String(error?.message ?? error) }));
  process.exit(0);
}

async function saveSheets(sheets, outDir, prefix) {
  mkdirSync(outDir, { recursive: true });
  const images = [];
  let index = 0;
  for (const sheet of sheets) {
    index += 1;
    const file = join(outDir, prefix + '-' + index + '.png');
    writeFileSync(file, sheet.png);
    images.push({ path: file, bytes: sheet.png.length, width: sheet.width ?? null, height: sheet.height ?? null });
  }
  return images;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const specPath = process.argv[2];
  if (!specPath) return fail('missing spec path');
  const spec = JSON.parse(readFileSync(specPath, 'utf8'));
  const args = spec.args || {};
  const outDir = spec.outDir;
  const ffmpegPath = spec.ffmpegPath || undefined;
  const ffprobePath = spec.ffprobePath || undefined;
  const prefix = spec.prefix || ('sb-' + Date.now());
  const tool = spec.tool;

  if (tool === 'probe') {
    const fetchOptions = { cookies: args.cookies, allowAnonymous: args.allowAnonymous === true };
    if (isHttpUrl(args.input)) {
      const remote = await probeUrl(args.input, fetchOptions);
      process.stdout.write(JSON.stringify({ ok: true, images: [], text: formatRemoteProbe(remote) }));
      return;
    }
    const info = { ...(await probeVideo(args.input, { ffprobePath })), path: args.input };
    process.stdout.write(JSON.stringify({ ok: true, images: [], text: await formatProbe(info) }));
    return;
  }

  if (tool === 'locate') {
    const resolved = await resolveInput(args.input, { cookies: args.cookies, allowAnonymous: args.allowAnonymous === true });
    let segments = recallSegments(resolved.path);
    if (!segments) {
      await runStoryboard({
        input: resolved.path, from: args.from, to: args.to, cells: 1,
        transcriptFile: args.transcriptFile, textOnly: true, encode: false, ffmpegPath, ffprobePath,
      });
      segments = recallSegments(resolved.path);
    }
    if (!segments || segments.length === 0) {
      process.stdout.write(JSON.stringify({ ok: true, images: [], text: 'no transcript available for this file. Run video_storyboard first, or pass transcriptFile.' }));
      return;
    }
    const lo = Number.isFinite(args.from) ? args.from : 0;
    const hi = Number.isFinite(args.to) ? args.to : Infinity;
    const inWindow = segments.filter((x) => x.to >= lo && x.from <= hi);
    const matches = matchSegments(inWindow, args.query, { max: Math.min(20, Math.max(1, Number(args.max) || 5)) });
    process.stdout.write(JSON.stringify({ ok: true, images: [], text: formatMatches(args.query, matches, { total: inWindow.length }) }));
    return;
  }

  if (tool === 'zoom') {
    let from = args.from;
    let to = args.to;
    if (args.cellId !== undefined && args.cellId !== null) {
      const hit = await lookupCell(args.input, args.cellId);
      if (!hit) throw new Error('no cell map recorded for this input (cellId ' + args.cellId + '). Run video_storyboard first, or pass from/to.');
      from = hit.from; to = hit.to;
    }
    const result = await runStoryboard({
      input: args.input, from, to, cells: args.cells ?? 6, sheets: 1,
      minGap: args.minGap ?? 1.2, pick: args.pick ?? 'sharp', burn: args.burn ?? 'index',
      transcript: args.transcript ?? 'auto', sourceHash: args.sourceHash === true, adaptive: false,
      ffmpegPath, ffprobePath,
      fetchOptions: {
        cookies: args.cookies, allowAnonymous: args.allowAnonymous === true,
        scanHeight: 360, zoomHeight: args.zoomHeight ?? 1080,
      },
    });
    const images = await saveSheets(result.sheets, outDir, prefix);
    const text = formatManifest(result, { maxCellChars: DETAIL_CHARS.med });
    process.stdout.write(JSON.stringify({ ok: true, images, text }));
    return;
  }

  // 默认：storyboard
  const result = await runStoryboard({
    input: args.input,
    from: args.from ?? 0,
    to: args.to ?? 0,
    cells: args.cells ?? 0,
    sheets: args.sheets ?? 0,
    pick: args.pick ?? 'sharp',
    burn: args.burn ?? 'index',
    minGap: args.minGap ?? 1.2,
    transcript: args.transcript ?? 'auto',
    transcriptFile: args.transcriptFile,
    ...(typeof args.hotwords === 'string' ? { hotwords: args.hotwords } : {}),
    sourceHash: args.sourceHash === true,
    textOnly: args.textOnly === true,
    detail: args.detail ?? 'med',
    ocr: args.ocr === true || (args.ocr !== false && (args.detail ?? 'med') === 'high'),
    ...(args.planThresholds && typeof args.planThresholds === 'object' ? { planThresholds: args.planThresholds } : {}),
    ffmpegPath, ffprobePath,
    fetchOptions: { cookies: args.cookies, allowAnonymous: args.allowAnonymous === true, scanHeight: args.scanHeight ?? 360 },
  }, [], args.cellRange ?? null);
  const images = args.textOnly === true ? [] : await saveSheets(result.sheets, outDir, prefix);
  const text = formatManifest(result, { cellRange: args.cellRange ?? null, maxCellChars: DETAIL_CHARS[args.detail ?? 'med'] ?? DETAIL_CHARS.med });
  process.stdout.write(JSON.stringify({ ok: true, images, text }));
}

main().then(() => sleep(0)).catch((error) => fail(error));

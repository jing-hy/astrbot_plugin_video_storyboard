import { writeFileSync } from 'node:fs';
writeFileSync(process.argv[2], JSON.stringify({
  tool: 'storyboard',
  args: { input: process.argv[3], cells: 4, transcript: 'off' },
  outDir: process.argv[4],
  prefix: 'test',
}));

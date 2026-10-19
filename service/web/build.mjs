import {build} from 'esbuild';
import {mkdir, copyFile} from 'node:fs/promises';
await mkdir('dist', {recursive:true});
await build({entryPoints:['src/app.js'], bundle:true, minify:true, format:'esm', outdir:'dist', target:['es2022'], legalComments:'none'});
await copyFile('src/index.html','dist/index.html');

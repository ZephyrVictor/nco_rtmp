import {chromium} from '@playwright/test';
import {readFile,writeFile} from 'node:fs/promises';
const password=(await readFile(new URL('../../.admin-password',import.meta.url),'utf8')).trim();
const browser=await chromium.launch({channel:'chrome',headless:true});
const errors=[];
try{
 const page=await browser.newPage({viewport:{width:1440,height:1100},ignoreHTTPSErrors:true});
 page.on('pageerror',e=>errors.push(e.message));
 page.on('console',m=>{if(m.type()==='error')console.log('BROWSER_ERROR',m.text());});
 page.on('response',r=>{if(r.url().includes('.flv'))console.log('FLV_STATUS',r.status());});
 await page.goto('https://127.0.0.1:18443');
 if(await page.evaluate(()=>performance.getEntriesByType('navigation')[0].nextHopProtocol)!=='h2')throw Error('HTTP/2 required');
 await page.getByLabel('账号',{exact:true}).fill('admin');
 await page.getByLabel('密码',{exact:true}).fill(password);
 await page.getByRole('button',{name:'登录',exact:true}).click();
 await page.locator('#live-only').check();
 try {await page.waitForFunction(()=>document.querySelectorAll('.stream video').length===16&&[...document.querySelectorAll('.stream video')].every(v=>v.readyState>=2&&v.videoWidth>0),null,{timeout:40000});}
 catch(error){console.log('PLAYER_DEBUG',await page.locator('.stream').evaluateAll(nodes=>nodes.map(n=>({id:n.dataset.cid,hint:n.querySelector('.placeholder').textContent,status:n.querySelector('.status').textContent,ready:n.querySelector('video').readyState,time:n.querySelector('video').currentTime,error:n.querySelector('video').error?.message}))));await page.screenshot({path:new URL('../../test-results/load-wall-debug.png',import.meta.url).pathname,fullPage:true});throw error;}
 const frames=await page.locator('.stream video').evaluateAll(videos=>videos.map(v=>{const c=document.createElement('canvas');c.width=80;c.height=52;const x=c.getContext('2d');x.drawImage(v,0,0,80,52);const a=x.getImageData(0,0,80,52).data;let min=255,max=0;for(let i=0;i<a.length;i+=4){min=Math.min(min,a[i]);max=Math.max(max,a[i]);}return {width:v.videoWidth,height:v.videoHeight,currentTime:v.currentTime,range:max-min};}));
 if(frames.some(f=>f.range<20))throw Error('Blank player');
 await page.screenshot({path:new URL('../../test-results/load-wall-16.png',import.meta.url).pathname,fullPage:true});
 const oldIds=await page.locator('.stream').evaluateAll(nodes=>nodes.map(n=>n.dataset.cid));
 await page.getByRole('button',{name:'下一页',exact:true}).click();
 await page.waitForFunction(ids=>document.querySelectorAll('.stream video').length===16&&[...document.querySelectorAll('.stream')].every(n=>!ids.includes(n.dataset.cid))&&[...document.querySelectorAll('.stream video')].every(v=>v.readyState>=2),oldIds,{timeout:45000});
 if(errors.length)throw Error(errors.join('\n'));
 const result={checks:['16-concurrent-decoded-nonblank-videos','pagination-replaces-players'],frames,errors};
 await writeFile(new URL('../../test-results/browser-load-results.json',import.meta.url),JSON.stringify(result,null,2));
 console.log(JSON.stringify(result));
}finally{await browser.close();}

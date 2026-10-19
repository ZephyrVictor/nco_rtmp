import {chromium} from '@playwright/test';
import {readFile,writeFile} from 'node:fs/promises';
const cid=process.env.FAULT_CID;
if(!cid)throw Error('FAULT_CID required');
const password=(await readFile(new URL('../../.admin-password',import.meta.url),'utf8')).trim();
const browser=await chromium.launch({channel:'chrome',headless:true});
const errors=[];
try{
 const page=await browser.newPage({viewport:{width:1280,height:900},ignoreHTTPSErrors:true});
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto('https://127.0.0.1:18443');
 await page.getByLabel('账号',{exact:true}).fill('admin');
 await page.getByLabel('密码',{exact:true}).fill(password);
 await page.getByRole('button',{name:'登录',exact:true}).click();
 await page.getByRole('button',{name:'原件提交',exact:true}).click();
 await page.locator('#upload-form select[name="contestant_id"]').selectOption(cid);
 await page.locator('#upload-form input[type="file"]').setInputFiles(new URL('../../test-results/fault-original.mkv',import.meta.url).pathname);
 await page.getByRole('button',{name:'上传原件',exact:true}).click();
 await page.getByText('已接收，等待校验',{exact:false}).waitFor({timeout:90000});
 if(errors.length)throw Error(errors.join('\n'));
 await page.screenshot({path:new URL('../../test-results/browser-upload.png',import.meta.url).pathname,fullPage:true});
 const result={checks:['browser-sha256-wasm','browser-tus-upload-completion'],errors};
 await writeFile(new URL('../../test-results/browser-upload-results.json',import.meta.url),JSON.stringify(result,null,2));
 console.log(JSON.stringify(result));
}finally{await browser.close();}

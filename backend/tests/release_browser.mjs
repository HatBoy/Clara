// 仅绑定回环随机端口；把页面内 API 请求送到本轮测试后端，绝不回退到开发端口。
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';
const root = fileURLToPath(new URL('../../', import.meta.url));
const require = createRequire(import.meta.url);
const pw = process.env.CLARA_QA_PLAYWRIGHT || join(execFileSync(process.platform === 'win32' ? 'npm.cmd' : 'npm', ['root','-g'], {encoding:'utf8', shell:process.platform === 'win32'}).trim(), '@playwright/cli/node_modules/playwright');
const { chromium } = require(pw);
const port = Number(process.argv[2]);
if (!Number.isInteger(port) || port < 1 || port === 8000) throw Error('需要隔离的后端端口');
const vite = await createServer({root, server:{host:'127.0.0.1', port:0, open:false, hmr:false, proxy:{'/api':`http://127.0.0.1:${port}`}}, plugins:[{name:'验收专用地址',enforce:'pre',transform(code,id){if(id.endsWith('/src/lib/api.ts'))return code.replace('export let API_BASE = "http://127.0.0.1:8000"','export let API_BASE = location.origin');}}]});
let browser;
try {
  await vite.listen();
  const origin=`http://127.0.0.1:${vite.httpServer.address().port}`;
  browser=await chromium.launch({executablePath:process.env.CLARA_QA_BROWSER || undefined});
  const page=await browser.newPage();
  await page.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
  await page.goto(origin,{waitUntil:'domcontentloaded'});
  const skip=page.getByRole('button',{name:'跳过引导'});
  await skip.click();
  const input=page.getByPlaceholder(/^添加日程/);
  const title='发版验收浏览器事项';
  await input.fill(title);
  const response=page.waitForResponse(r=>r.url()===`${origin}/api/tasks` && r.request().method()==='POST');
  await input.press('Enter');
  if (!(await response).ok()) throw Error('录入失败');
  await page.reload({waitUntil:'domcontentloaded'});
  await page.getByText("收件箱",{exact:true}).first().click();
  await page.getByText(title,{exact:true}).first().waitFor();
  const rows=await (await page.request.get(`${origin}/api/tasks?unscheduled=true`)).json();
  if(rows.filter(r=>r.title===title && !r.start_time).length!==1)throw Error('日历与收件箱持久态不符');
  console.log('浏览器：录入、重载、收件箱唯一任务通过');
} finally {await browser?.close();await vite.close();}

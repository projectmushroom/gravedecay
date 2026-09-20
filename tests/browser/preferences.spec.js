const {test,expect}=require('@playwright/test');
test.use({serviceWorkers:'block'});

async function fixture(page,request,baseURL,options={}){
  const original=await (await request.get(new URL('api/v1/resources/preferences',baseURL).href,{headers:{'X-Grave-Client':'1'}})).json();
  const writes=[],legacy=[];
  await page.route('**/api/v1/resources/preferences',async route=>{
    if(options.unavailable)return route.fulfill({status:503,json:{ok:false,error:'resource_unavailable',output:'Cannot read preferences'}});
    if(route.request().method()==='POST'){
      const payload=route.request().postDataJSON();writes.push(payload);
      if(options.conflict)return route.fulfill({status:409,json:{ok:false,error:'revision_conflict',output:'Preferences changed on this grave. Reopen Dashboard preferences and review before saving.'}});
      return route.fulfill({json:{...original,data:{revision:'b'.repeat(64),values:{...original.data.values,...payload.changes}}}});
    }
    return route.fulfill({json:original});
  });
  await page.route('**/api/settings',route=>{
    legacy.push(route.request().postDataJSON());
    return route.fulfill({json:{ok:true,settings:{...original.data.values,...legacy.at(-1)},linear_configured:false}});
  });
  if(options.old){
    await page.route('**/api/v1/capabilities',async route=>{
      const response=await route.fetch();const caps=await response.json();
      caps.routes.GET=caps.routes.GET.filter(n=>!n.startsWith('resources/'));
      caps.routes.POST=caps.routes.POST.filter(n=>!n.startsWith('resources/'));
      delete caps.resource_contract;
      return route.fulfill({response,json:caps});
    });
  }
  await page.goto('./');
  await page.locator('[data-tab=system]').click();
  await page.locator('[data-settings=dashboard]').click();
  return {writes,legacy,original};
}

test('preference patch carries its read revision and preserves newly added tiles',async({page,request,baseURL})=>{
  const {writes,legacy,original}=await fixture(page,request,baseURL);
  await expect(page.locator('#save-set')).toBeEnabled();
  await page.locator('[data-sec=sec-apps]').click();
  await page.locator('#new-app-name').fill('Docs');
  await page.locator('#new-app-url').fill('/docs/');
  await page.locator('#add-app').click();
  await page.locator('#set-poll').selectOption('10000');
  await page.locator('#save-set').click();
  await expect(page.locator('#set-msg')).toHaveText('saved ✓');
  expect(writes).toHaveLength(1);
  expect(writes[0].revision).toBe(original.data.revision);
  expect(writes[0].changes.custom_apps).toContainEqual({name:'Docs',url:'/docs/'});
  expect(writes[0].changes.poll_ms).toBe(10000);
  expect(writes[0].changes.linear_key).toBeUndefined();
  expect(writes[0].changes.notify_events).toBeUndefined();
  expect(legacy).toEqual([]);
});

test('a conflicting save keeps the draft and never retries through legacy settings',async({page,request,baseURL})=>{
  const {writes,legacy}=await fixture(page,request,baseURL,{conflict:true});
  await expect(page.locator('#save-set')).toBeEnabled();
  await page.locator('#set-poll').selectOption('10000');
  await page.locator('#save-set').click();
  await expect(page.locator('#set-msg')).toContainText('Preferences changed on this grave');
  await expect(page.locator('#set-poll')).toHaveValue('10000');
  expect(writes).toHaveLength(1);expect(legacy).toEqual([]);
});

test('resource failure blocks saving, while an explicitly older peer keeps compatibility',async({page,request,baseURL})=>{
  const result=await fixture(page,request,baseURL,{unavailable:true});
  await expect(page.locator('#set-msg')).toContainText('Cannot load preferences');
  await expect(page.locator('#save-set')).toBeDisabled();
  expect(result.writes).toEqual([]);expect(result.legacy).toEqual([]);
  await page.unrouteAll({behavior:'ignoreErrors'});
  const old=await fixture(page,request,baseURL,{old:true});
  await expect(page.locator('#save-set')).toBeEnabled();
  await page.locator('#save-set').click();
  await expect(page.locator('#set-msg')).toHaveText('saved ✓');
  expect(old.legacy).toHaveLength(1);expect(old.writes).toEqual([]);
});

const { test, expect } = require('@playwright/test');
// The negative control edits response headers through routing; a service
// worker would bypass that route and keep the original policy in place.
test.use({ serviceWorkers: 'block' });

test('dashboard loads normally but refuses framing with its response policy', async ({ page, baseURL }) => {
  const target = new URL('/', baseURL).href;
  await page.goto(target);
  await expect(page.locator('#plot-context')).toBeVisible();

  // A routed loopback origin needs no second listener and avoids unrelated
  // public-to-private network restrictions interfering with the framing test.
  const attackerURL = new URL('/frame-attempt', target);
  attackerURL.port = attackerURL.port === '3999' ? '3998' : '3999';
  const attacker = attackerURL.href;
  await page.route(attacker, route => route.fulfill({
    contentType: 'text/html',
    body: `<title>frame attempt</title><iframe src="${target}" onload="window.frameFinished=true"></iframe>`,
  }));
  let stripPolicy = false;
  let attempts = 0;
  await page.route(target, async route => {
    // Fetch the real fixture response. The negative control removes only the
    // frame headers to prove that an empty iframe isn't a connectivity failure.
    const response = await route.fetch();
    expect(response.status()).toBe(200);
    const headers = response.headers();
    expect(headers['content-security-policy']).toBe("frame-ancestors 'none'");
    expect(headers['x-frame-options']).toBe('DENY');
    if (stripPolicy) {
      delete headers['content-security-policy'];
      delete headers['x-frame-options'];
    }
    attempts++;
    await route.fulfill({ response, headers });
  });
  await page.goto(attacker);
  await page.waitForFunction(() => window.frameFinished === true);
  expect(attempts).toBeGreaterThan(0);
  await expect(page.frameLocator('iframe').locator('#plot-context')).toHaveCount(0);

  stripPolicy = true;
  await page.reload();
  await expect(page.frameLocator('iframe').locator('#plot-context')).toBeVisible();

  await page.unroute(target);
  await page.goto(target);
  await expect(page.locator('#plot-context')).toBeVisible();
});

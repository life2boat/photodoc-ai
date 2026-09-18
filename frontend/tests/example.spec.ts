import { test, expect } from '@playwright/test';

test.describe('PhotoDoc Smoke Tests', () => {
  test('opens landing page, body is visible, and no horizontal overflow', async ({ page }) => {
    const response = await page.goto('/');
    expect(response?.status()).toBeLessThan(400);

    const body = page.locator('body');
    await expect(body).toBeVisible();

    const hasHorizontalOverflow = await page.evaluate(() => {
      return document.documentElement.scrollWidth > document.documentElement.clientWidth;
    });
    expect(hasHorizontalOverflow).toBe(false);
  });
});
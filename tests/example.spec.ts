import { test, expect } from '@playwright/test';

test('has title', async ({ page }) => {
  await page.goto('http://localhost:5173/');

  await expect(page).toHaveTitle(/PhotoDoc AI/);
});

test('shows PhotoDoc landing page', async ({ page }) => {
  await page.goto('http://localhost:5173/');

  await expect(page.getByText('PhotoDoc').first()).toBeVisible();
});

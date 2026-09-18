import { test, expect } from '@playwright/test';

test.describe('Desktop Smoke Tests (1440x900)', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test('loads page with H1, CTA, services, examples nav and no horizontal overflow', async ({ page }) => {
    await page.goto('/');

    // Page loads and title is correct
    await expect(page).toHaveTitle(/PhotoDoc AI/);

    // EXACTLY ONE H1
    const h1 = page.locator('h1');
    await expect(h1).toHaveCount(1);
    await expect(h1).toBeVisible();
    await expect(h1).toContainText('Цифровая фотостудия нового поколения');

    // CTA button visible in Hero
    const ctaButton = page.getByRole('button', { name: /Начать заказ/i });
    await expect(ctaButton).toBeVisible();

    // Services section visible
    const servicesSection = page.locator('#services-section');
    await expect(servicesSection).toBeVisible();

    // Examples navigation scrolls to showcase
    const examplesBtn = page.locator('header nav').getByRole('button', { name: /Примеры/i });
    await examplesBtn.click();
    const showcase = page.locator('#examples-showcase');
    await expect(showcase).toBeInViewport();

    // Check no horizontal overflow
    const hasHorizontalOverflow = await page.evaluate(() => {
      return document.documentElement.scrollWidth > document.documentElement.clientWidth;
    });
    expect(hasHorizontalOverflow).toBe(false);
  });
});

test.describe('Mobile Smoke Tests (390x844)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('loads mobile layout with H1, mobile nav, CTA and no horizontal overflow', async ({ page }) => {
    await page.goto('/');

    // H1 visible on mobile
    const h1 = page.locator('h1');
    await expect(h1).toHaveCount(1);
    await expect(h1).toBeVisible();

    // CTA visible on mobile
    const ctaButton = page.getByRole('button', { name: /Начать заказ/i });
    await expect(ctaButton).toBeVisible();

    // Mobile nav toggle exists and opens navigation
    const menuToggle = page.getByRole('button', { name: /Открыть меню/i });
    await expect(menuToggle).toBeVisible();
    await menuToggle.click();

    const mobileNav = page.locator('nav[aria-label="Мобильная навигация"]');
    await expect(mobileNav).toBeVisible();
    await expect(mobileNav.getByRole('button', { name: /Услуги/i })).toBeVisible();
    await expect(mobileNav.getByRole('button', { name: /Примеры/i })).toBeVisible();
    await expect(mobileNav.getByRole('button', { name: /Оформить заказ/i })).toBeVisible();

    // Click link in mobile nav closes menu
    await mobileNav.getByRole('button', { name: /Примеры/i }).click();
    await expect(mobileNav).not.toBeVisible();

    // Check no horizontal overflow
    const hasHorizontalOverflow = await page.evaluate(() => {
      return document.documentElement.scrollWidth > document.documentElement.clientWidth;
    });
    expect(hasHorizontalOverflow).toBe(false);
  });
});

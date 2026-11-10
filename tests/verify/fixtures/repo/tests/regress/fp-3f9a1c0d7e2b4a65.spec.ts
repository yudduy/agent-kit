import { test, expect } from '@playwright/test';

test('a saved name is still there after a reload', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Name').fill('Ada');
  await page.getByRole('button', { name: 'Save' }).click();
  await page.goto('/');
  await expect(page.locator('#current')).toHaveText('Ada');
});

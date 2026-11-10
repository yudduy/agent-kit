import { test, expect } from '@playwright/test';

test('rename the profile', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Name').fill('Ada');
  await page.getByRole('button', { name: 'Save' }).click();
  await expect(page.locator('#status')).toHaveText('Saved');
});

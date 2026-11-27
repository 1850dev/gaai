import { expect, test } from '@playwright/test';

test.describe('public OAuth branding pages', () => {
  test.use({ javaScriptEnabled: false, viewport: { width: 375, height: 800 } });

  test('homepage and policies are linked and readable without Firebase or JavaScript', async ({
    page
  }) => {
    expect((await page.goto('/about'))?.status()).toBe(200);
    await expect(page.getByRole('heading', { name: 'Analytics AI', level: 1 })).toBeVisible();
    await expect(page.getByText('ページ別ランキングや期間比較', { exact: false })).toBeVisible();
    await page.getByRole('link', { name: 'プライバシーポリシー', exact: true }).first().click();
    await expect(page).toHaveURL(/\/privacy$/);
    await expect(
      page.getByRole('heading', { name: 'プライバシーポリシー', level: 1 })
    ).toBeVisible();
    await expect(page.locator('code')).toHaveText(
      'https://www.googleapis.com/auth/analytics.readonly'
    );
    await expect(page.getByText('自動削除されません', { exact: false })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      375
    );
    await page.getByRole('link', { name: '利用規約' }).click();
    await expect(page).toHaveURL(/\/terms$/);
    await expect(page.getByRole('heading', { name: '利用規約', level: 1 })).toBeVisible();
  });
});

test('shows a safe configuration gate without Firebase settings', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('Configuration required')).toBeVisible();
  await expect(page.getByText('Firebaseの公開設定')).toBeVisible();
});

test('settings page requires login before showing personal configuration', async ({ page }) => {
  await page.goto('/settings');
  await expect(page.getByRole('heading', { name: 'サイト設定・定期レポート' })).toBeVisible();
  await expect(page.getByText('分析画面からログインしてください。')).toBeVisible();
  await expect(page.getByRole('button', { name: 'サイト設定を保存' })).toHaveCount(0);
});

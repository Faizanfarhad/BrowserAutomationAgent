import asyncio
from playwright.async_api import async_playwright, Playwright


async def run(playwright: Playwright):
    url = "https://www.wikipedia.org/"
    target = "search-input"
    chromium = playwright.chromium # or "firefox" or "webkit".
    browser = await chromium.launch(headless=False)
    page = await browser.new_page()
    await page.goto(url)
    # other actions...
    locator = page.locator(f'input[id={target}]')
    count = await locator.count()
    print(count)
    
    locator = page.get_by_alt_text(f"{target}")
    count = await locator.count()
    print(count)
    
    
    locator = page.get_by_label(f"{target}")
    count = await locator.count()
    print(count)
    
    
    locator = page.get_by_role(f"{target}")
    count = await locator.count()
    print(count)
    
    await page.wait_for_timeout(4000)
    await page.screenshot(path="traces/img.png")
    await browser.close()



async def main():
    async with async_playwright() as playwright:
        await run(playwright)
        
asyncio.run(main())


# print(text)
from playwright.async_api import Playwright

async def find_relevant_element(
    playwright: Playwright,
    current_url: str,
    role: str,
):
    """Collect matching element details in a separate headless browser."""

    browser = await playwright.chromium.launch(headless=True)
    try:
        context = await browser.new_context()
        try:
            observation_page = await context.new_page()
            await observation_page.goto(current_url)

            elements = observation_page.get_by_role(role)
            total_relevant = await elements.count()
            relevant_data = {}

            for index in range(total_relevant):
                element = elements.nth(index)
                
                
                try:
                    is_visible = await element.is_visible(),
                except:
                    is_visible = False
                try:
                    is_enabled = await element.is_enabled(),
                except:
                    is_enabled = False
                try:
                    is_editable = await element.is_editable()
                except:
                    is_editable = False
                 
                relevant_data[f"relevant{index}"] = {
                    "tag": await element.evaluate("el => el.tagName.toLowerCase()"),
                    "id": await element.get_attribute("id"),
                    "type": await element.get_attribute("type"),
                    "name": await element.get_attribute("name"),
                    "placeholder": await element.get_attribute("placeholder"),
                    "visible": is_visible,
                    "enabled": is_enabled,
                    "editable":  is_editable ,
                }

            first_match = relevant_data.get("relevant0", {})
            return {
                "tag": first_match.get("tag"),
                "role": role,
                "total_relevant": total_relevant,
                "relevants": relevant_data,
                "visible": first_match.get("visible", False),
                "enable": first_match.get("enabled", False),
                "editable": first_match.get("editable", False),
            }
        finally:
            await context.close()
    finally:
        await browser.close()

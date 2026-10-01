from playwright.async_api import TimeoutError as PlaywrightTimeoutError


async def extract_categorized(page, category, fields, query=None):
    """Extract requested fields from semantic and structured page data."""
    if "product" in category.lower():
        try:
            await page.wait_for_function(
                """() => {
                    const text = document.body?.innerText || "";
                    if (/unusual traffic|verify (?:that )?you are human|not a robot|not a bot|security check|captcha|quick check before you continue/i.test(text)) {
                        return true;
                    }
                    return [...document.querySelectorAll("a[href]")]
                        .some(link => /(?:₹|\\$|€|£|\\bRs\\.?\\s*)\\s*[\\d,]+/i.test(link.innerText));
                }""",
                timeout=8000,
            )
        except PlaywrightTimeoutError:
            pass

    return await page.evaluate(
        """({ category, fields, query }) => {
            const normalize = value => String(value || "")
                .toLowerCase()
                .replace(/[^a-z0-9]/g, "");

            const aliasesFor = field => {
                const key = normalize(field);
                const aliases = new Set([key]);
                const groups = [
                    ["title", "headline", "name"],
                    ["author", "authors", "creator"],
                    ["abstract", "description", "summary"],
                    ["publicationdate", "datepublished", "datecreated"],
                    ["pdfurl", "contenturl", "encodingurl"],
                ];
                for (const group of groups) {
                    if (group.includes(key)) {
                        group.forEach(alias => aliases.add(alias));
                    }
                }
                return aliases;
            };

            const toText = value => {
                if (value == null) return "";
                if (Array.isArray(value)) {
                    return value.map(toText).filter(Boolean).join(", ");
                }
                if (typeof value === "object") {
                    return toText(value.name ?? value.value ?? value.text ?? value.url);
                }
                return String(value).trim();
            };

            const isUrlField = field => /(?:url|link|href)$/i.test(field);
            const isVisibleInDom = element =>
                !element.closest("[hidden], [aria-hidden='true'], [inert]");

            const valueFromElement = (element, field) => {
                const tag = element.tagName.toLowerCase();
                if (tag === "dt") {
                    return element.nextElementSibling?.tagName.toLowerCase() === "dd"
                        ? element.nextElementSibling.innerText
                        : "";
                }
                if (tag === "th") {
                    const row = element.closest("tr");
                    const headers = Array.from(row?.querySelectorAll("th") || []);
                    const cellIndex = headers.indexOf(element);
                    return row?.children[cellIndex + 1]?.innerText || "";
                }
                if (tag === "label") {
                    const control = element.control || element.querySelector("input, textarea, select");
                    if (control) return control.value || control.innerText || "";
                }
                if (tag === "meta") return element.content || "";
                if (tag === "time") return element.dateTime || element.innerText;
                if (tag === "a" && isUrlField(field)) return element.href;
                if (["input", "textarea", "select"].includes(tag)) {
                    return element.value || "";
                }
                return element.getAttribute("content") || element.innerText || element.textContent;
            };

            const jsonLdValues = aliases => {
                const values = [];
                const visit = value => {
                    if (Array.isArray(value)) {
                        value.forEach(visit);
                        return;
                    }
                    if (!value || typeof value !== "object") return;
                    for (const [key, nested] of Object.entries(value)) {
                        if (aliases.has(normalize(key))) values.push(toText(nested));
                        visit(nested);
                    }
                };
                for (const script of document.querySelectorAll(
                    "script[type='application/ld+json']"
                )) {
                    try {
                        visit(JSON.parse(script.textContent));
                    } catch {
                        continue;
                    }
                }
                return values;
            };

            const extractProductListings = () => {
                const amountPattern = /(₹|Rs\\.?|\\$|€|£)\\s*([\\d,]+(?:\\.\\d{1,2})?)/g;
                const stopWords = new Set([
                    "fetch", "find", "current", "price", "of", "the", "a", "an",
                    "laptop", "laptops", "product", "products", "for", "any", "show",
                    "me", "what", "are", "is", "buy", "online",
                ]);
                const pageQuery = new URL(location.href).searchParams;
                const queryText = query || [
                    ...["q", "st", "d", "query", "search", "k"].map(key => pageQuery.get(key)),
                ].filter(Boolean).join(" ");
                const searchTerms = (queryText.toLowerCase().match(/[a-z0-9]+/g) || [])
                    .filter(term => !stopWords.has(term));
                const records = [];
                const seenUrls = new Set();
                for (const link of document.querySelectorAll("a[href]")) {
                    const text = (link.innerText || "").trim();
                    const prices = [...text.matchAll(amountPattern)];
                    let productUrl = link.href;
                    const linkUrl = new URL(link.href);
                    if (linkUrl.hostname.endsWith("duckduckgo.com")) {
                        const destination = linkUrl.searchParams.get("uddg");
                        if (destination) productUrl = new URL(destination).href;
                    }
                    if (prices.length === 0 || text.length < 25 || seenUrls.has(productUrl)) {
                        continue;
                    }
                    const imageName = link.querySelector("img")?.alt?.trim();
                    const searchableText = normalize(
                        `${text} ${imageName || ""} ${new URL(productUrl).pathname}`
                    );
                    if (searchTerms.some(term => !searchableText.includes(normalize(term)))) {
                        continue;
                    }
                    seenUrls.add(productUrl);

                    const nameLine = text.split(/\\n/)
                        .map(line => line.trim())
                        .find(line => line.length > 15 && !/add to compare/i.test(line));
                    const productName = imageName || (nameLine
                        ? nameLine
                            .replace(/^Buy\\s+/i, "")
                            .split(/\\s+(?:Online\\s+)?For\\s+(?:Rs\\.?|₹|\\$|€|£)/i)[0]
                            .split(/,\\s*Also get\\b/i)[0]
                        : null);
                    const priceToken = prices[0][1];
                    const price = Number(prices[0][2].replace(/,/g, ""));
                    const currency = priceToken === "₹" || /^Rs/i.test(priceToken)
                        ? "INR"
                        : priceToken === "$" ? "USD"
                        : priceToken === "€" ? "EUR"
                        : priceToken === "£" ? "GBP"
                        : null;
                    const host = new URL(productUrl).hostname.replace(/^www\\./i, "");
                    const retailer = host.split(".")[0]
                        .replace(/[-_]/g, " ")
                        .replace(/\\b\\w/g, character => character.toUpperCase());
                    const values = {
                        product_name: productName,
                        price: Number.isFinite(price) ? price : null,
                        currency,
                        retailer,
                        product_url: productUrl,
                    };
                    const item = {};
                    for (const field of fields) {
                        const key = normalize(field);
                        if (["productname", "name", "title"].includes(key)) {
                            item[field] = values.product_name;
                        } else if (["price", "currentprice", "saleprice"].includes(key)) {
                            item[field] = values.price;
                        } else if (key === "currency") {
                            item[field] = values.currency;
                        } else if (key === "retailer" || key === "store") {
                            item[field] = values.retailer;
                        } else if (["producturl", "url", "link", "href"].includes(key)) {
                            item[field] = values.product_url;
                        } else {
                            item[field] = null;
                        }
                    }
                    records.push(item);
                }
                return records;
            };

            const extractField = field => {
                const aliases = aliasesFor(field);
                const values = jsonLdValues(aliases);

                for (const meta of document.querySelectorAll("meta")) {
                    const keys = ["name", "property", "itemprop"]
                        .map(attribute => meta.getAttribute(attribute))
                        .filter(Boolean);
                    if (keys.some(key => aliases.has(normalize(key)))) {
                        values.push(valueFromElement(meta, field));
                    }
                }

                const selector = [
                    "[itemprop]", "[aria-label]", "[data-field]", "[name]",
                    "[id]", "[data-testid]", "[class]", "dt", "th", "label",
                    "time", "a",
                ].join(",");
                for (const element of document.querySelectorAll(selector)) {
                    if (!isVisibleInDom(element)) continue;
                    const attributes = [
                        "itemprop", "aria-label", "data-field", "name", "id",
                        "data-testid", "class",
                    ];
                    const keys = attributes
                        .map(attribute => element.getAttribute(attribute))
                        .filter(Boolean)
                        .flatMap(value => value.split(/\\s+/));
                    if (["dt", "th", "label"].includes(element.tagName.toLowerCase())) {
                        keys.push(element.innerText || element.textContent || "");
                    }
                    if (keys.some(key => aliases.has(normalize(key)))) {
                        values.push(valueFromElement(element, field));
                    }
                }

                if (!values.some(value => toText(value)) && aliases.has("title")) {
                    values.push(document.querySelector("h1")?.innerText || document.title);
                }
                if (!values.some(value => toText(value)) && normalize(field).includes("pdf")) {
                    const pdfLink = Array.from(document.querySelectorAll("a[href]"))
                        .find(link => /pdf/i.test(`${link.innerText} ${link.href}`));
                    if (pdfLink) values.push(pdfLink.href);
                }

                const uniqueValues = [...new Set(values.map(toText).filter(Boolean))];
                if (normalize(field).includes("author")) return uniqueValues;
                return uniqueValues[0] || null;
            };

            const data = {};
            const missingFields = [];
            for (const field of fields) {
                data[field] = extractField(field);
                if (data[field] == null || data[field] === "" || data[field].length === 0) {
                    missingFields.push(field);
                }
            }
            const pageText = document.body?.innerText || "";
            const humanVerification =
                /unusual traffic|verify (?:that )?you are human|not a robot|not a bot|security check|captcha|quick check before you continue/i.test(pageText) ||
                Boolean(document.querySelector(
                    "iframe[src*='captcha'], iframe[src*='recaptcha'], [id*='captcha'], [class*='captcha']"
                ));
            if (normalize(category).includes("product")) {
                const items = extractProductListings();
                const missingProductFields = fields.filter(field =>
                    !items.some(item => item[field] != null && item[field] !== "")
                );
                return {
                    category,
                    data: { items },
                    missing_fields: missingProductFields,
                    blocked: humanVerification
                        ? "The page is requesting human verification; automation will not attempt to solve it."
                        : null,
                };
            }
            return {
                category,
                data,
                missing_fields: missingFields,
                blocked: humanVerification
                    ? "The page is requesting human verification; automation will not attempt to solve it."
                    : null,
            };
        }""",
        {"category": category, "fields": fields, "query": query or ""},
    )
import ollama
from typing import Optional,Literal
from config import * 
import dotenv 
import os 
import httpx
import json
import asyncio
import re
from string import Template
from pathlib import Path

dotenv.load_dotenv()

DEEPSEEK_API = os.getenv("DEEPSEEK_API")
OPENAPI = os.getenv("OPENAPI")
GEMINI_API = os.getenv("GEMINI_API")

class ApiProviderError(Exception):
    """Raised when an external model provider cannot complete the request."""

class Planner:
    PLAN_RESPONSE_RETRIES = MAX_PLANNER_RETRIES

    def __init__(self,mode:Optional[str | Literal["Local","Api"]],replanning:bool = False):
        """_summary_

        Args:
            mode (Optional[str], optional): "Local" | "Api"
            _description_  Defaults to "Local".
        """
        super().__init__()
        assert mode == "Local" or mode == "Api", f"""mode only can be "Local" or "Api" not {mode}"""
                
        self.mode = mode
        self.full_content = None
        self.replanning = replanning
        
    def save_plan(self, plan):
      folder = Path("saved_plan")
      folder.mkdir(parents=True, exist_ok=True)

      prefix = "replan" if self.replanning else "plan"
      index = 0

      while (folder / f"{prefix}{index}.json").exists():
          index += 1

      path = folder / f"{prefix}{index}.json"

      if isinstance(plan, str):
          plan = json.loads(plan)

      with path.open("w", encoding="utf-8") as file:
          json.dump(plan, file, indent=4)

      return plan

    @staticmethod
    def parse_plan_response(text):
        """Parse a model response and require the expected plan structure."""
        candidate = text.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            candidate = "\n".join(lines).strip()

        plan = json.loads(candidate)
        if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
            raise ValueError('Plan response must be a JSON object with a "steps" list')
        if not plan["steps"]:
          raise ValueError('Plan "steps" must contain at least one action')
        if any(not isinstance(step, dict) for step in plan["steps"]):
            raise ValueError('Every item in the plan "steps" list must be an object')
        return plan

    def build_local_system_prompt(
        self,
        task,
        previous_plan=None,
        executed_steps=None,
        failed_step=None,
        validation_info=None,
        current_url=None,
        dom_observations=None,
    ):
        """Build a compact prompt tuned for the configured local model."""
        if not self.replanning:
          return """You are a browser task planner. Convert the user task into a short, ordered Playwright plan.

    Return ONLY valid JSON in this exact shape: {"steps": [...]}.
    Use only these actions:
    - navigate: {"action":"navigate","url":"https://..."}
    - fill: {"action":"fill","target":{"role":"textbox","name":"..."},"value":"..."}
    - press: {"action":"press","target":{"role":"textbox","name":"..."},"key":"Enter"}
    - click: {"action":"click","target":{"role":"link|button","name":"..."}}
    - select: {"action":"select","target":{"role":"combobox","name":"..."},"value":"..."}
    - wait: {"action":"wait","duration":1000}
    - extract: {"action":"extract","category":"...","fields":["..."]}
    - download: {"action":"download","url":"https://.../file.ext"}
    - api_request: {"action":"api_request","method":"GET","url":"https://api.example.com/..."}
    - gmail_search: {"action":"gmail_search","query":"is:unread","max_results":10}
    - gmail_send: {"action":"gmail_send","to":"person@example.com","subject":"...","body":"..."}

    Planning rules:
    1. Make one executable action per step and put steps in execution order.
    2. Use semantic role and accessible name for page controls. Use the searchbox role for search inputs when available. Never invent CSS selectors or element IDs.
    3. Navigate to a page before interacting with or extracting from it. Extract only information available on the current page.
    4. For search tasks, navigate to the search site, fill its search textbox, press Enter, then click the requested result. Do not replace a requested search-and-open flow with a guessed article URL.
    5. Put all requested fields from one page in a single extract step. For an article, request article_title, first_paragraph, and official_website_url together when all are requested. Do not split them into separate extracts.
        6. When asked to open a named link within an article, use a click step with target {"role":"link","name":"the exact link text"}, then extract the requested fields from the page it opens. If the task asks for both articles, include a second article_title and first_paragraph extraction after that click. Do not substitute an extract step with fields ["links"].
        7. For a URL discovered by extraction, reference its earlier result exactly as {"from":"stepN.data.field_name"}. N is the one-based index of the extract step in the FINAL steps array, counting every preceding action. The source step must be earlier than the consumer, and the field must be in that step's fields list. Never guess a discovered URL.
        8. For multi-page resource tasks, do not skip stages: search/open article; extract article fields; navigate using official_website_url; extract documentation_url; navigate using documentation_url; extract a direct resource_url; download using resource_url.
        9. A resource_url must point to a direct file, not a homepage or landing page. Prefer links ending in a file extension or marked as downloads.
        10. If the task requests a download, include a download step. Do not claim a download from navigation alone.
        11. For product lists, use category product_listing, include the specific query, and request product_name, price, currency, retailer, product_url.
        12. Do not add explanations or actions unsupported by the task.
        13. Use api_request only for a clearly identified read-only HTTP API endpoint. API requests do not need a navigate step; use browser actions when no API endpoint is available.
        14. Use gmail_search for Gmail read/search tasks. Its query uses Gmail search syntax, such as "is:unread" or "from:alice@example.com". Do not use it for sending, deleting, or modifying messages.
        15. Use gmail_send only when the user explicitly asks to send an email. Require the exact recipient, subject, and body from the task. The sender is the authenticated Gmail account; do not invent a from address. Do not use gmail_send for drafts, deleting, or modifying messages.

    Required sequence for a Wikipedia article followed by its official site's documentation and a direct download:
    {"steps":[
     {"action":"navigate","url":"https://en.wikipedia.org"},
     {"action":"fill","target":{"role":"searchbox","name":"Search Wikipedia"},"value":"Python"},
     {"action":"press","target":{"role":"searchbox","name":"Search Wikipedia"},"key":"Enter"},
     {"action":"click","target":{"role":"link","name":"Python (programming language)"}},
     {"action":"extract","category":"article","fields":["article_title","first_paragraph","official_website_url"]},
     {"action":"navigate","url":{"from":"step5.data.official_website_url"}},
     {"action":"extract","category":"site","fields":["documentation_url"]},
     {"action":"navigate","url":{"from":"step7.data.documentation_url"}},
     {"action":"extract","category":"downloadable_resource","fields":["resource_name","resource_url"]},
     {"action":"download","url":{"from":"step9.data.resource_url"}}
    ]}

    Reference audit for this example: step 5 produces official_website_url, step 6 uses step5; step 7 produces documentation_url, step 8 uses step7; step 9 produces resource_url, step 10 uses step9. Recount from the final steps array before returning any plan.

    Return a plan for the user's task message. Do not include prose outside the JSON object."""

        def as_json(value):
          return json.dumps(value, ensure_ascii=False, indent=2, default=str)

        return f"""You are repairing a browser automation plan. Return ONLY a JSON object with one field: "steps".
    Use only the supported actions: navigate, click, fill, press, select, wait, extract, download, api_request, gmail_search, gmail_send.
    Keep the original task's requested outputs and required download action. Do not invent URLs, targets, or selectors.
    URL values extracted earlier use {{"from":"stepN.data.field_name"}} with a one-based step number and a field requested by that extract step.
    The execution browser remains open during runtime replans. Use the current URL and DOM observations; include navigation only when needed to reach the intended page.
    For a blocked page, do not solve verification; choose a supported alternate source. For missing fields, use observations to improve extraction and keep the current source if it remains relevant.
    If resource_url is missing on a documentation hub and observations show a link labeled like "Download Current Documentation", navigate to that observed link, then extract the direct resource URL there. Do not retry the same extraction on the hub or treat its landing-page URL as a file.

    Original task:
    {task}

    Previous plan:
    {as_json(previous_plan)}

    Executed steps:
    {as_json(executed_steps)}

    Failed step:
    {as_json(failed_step)}

    Validation:
    {as_json(validation_info)}

    Current URL:
    {as_json(current_url)}

    DOM observations:
    {as_json(dom_observations)}

    Return the smallest valid corrected sequence needed to finish the original task. Preserve extraction fields and include extract before any URL reference that depends on it."""

    @staticmethod
    def local_plan_issues(task, plan):
        """Return missing task requirements or invalid step references in a plan."""
        steps = plan.get("steps", [])
        actions = {
            step.get("action") for step in steps if isinstance(step, dict)
        }
        requested_fields = {
            field.lower()
            for step in steps
            if isinstance(step, dict) and step.get("action") == "extract"
            for field in step.get("fields", [])
            if isinstance(field, str)
        }
        task_text = str(task).lower()
        issues = []

        if re.search(r"\bdownload(?:s|ed|ing)?\b", task_text) and "download" not in actions:
            issues.append("The task requests a file download, but the plan has no download action.")
        if re.search(r"\bextract(?:s|ed|ing|ion)?\b", task_text) and "extract" not in actions:
            issues.append("The task requests extraction, but the plan has no extract action.")

        required_field_checks = [
            (
                "article title",
                "article" in task_text and "title" in task_text,
                {"article_title", "title"},
            ),
            ("first paragraph", "first paragraph" in task_text, {"first_paragraph", "lead_paragraph"}),
            (
                "official website URL",
                bool(re.search(r"official (?:website|site).{0,50}url|url.{0,50}official (?:website|site)", task_text)),
                {"official_website_url", "official_site_url"},
            ),
            ("question number", "question number" in task_text, {"question_number"}),
            ("description", "description" in task_text, {"description"}),
        ]
        for label, requested, accepted_names in required_field_checks:
            if requested and not requested_fields.intersection(accepted_names):
                issues.append(f"The plan does not extract the requested {label}.")

        named_link = re.search(r"\blink to\s+\*{2}([^*]+)\*{2}", task_text)
        if named_link:
            target_name = re.sub(
                r"\bwithin\b.*",
                "",
                named_link.group(1),
                flags=re.IGNORECASE,
            ).strip()
            normalized_name = re.sub(r"[^a-z0-9]", "", target_name)
            article_extract_index = min(
                (
                    index
                    for index, step in enumerate(steps)
                    if isinstance(step, dict)
                    and step.get("action") == "extract"
                    and step.get("category") == "article"
                ),
                default=-1,
            )
            link_is_opened = any(
                isinstance(step, dict)
                and step.get("action") == "click"
                and isinstance(step.get("target"), dict)
                and re.sub(
                    r"[^a-z0-9]",
                    "",
                    str(step["target"].get("name", "")).lower(),
                ) == normalized_name
                and index > article_extract_index
                for index, step in enumerate(steps)
            )
            if not link_is_opened:
                issues.append(
                    f"After extracting the article, click the link named '{target_name}' instead of extracting a generic links field."
                )
            elif "new article" in task_text or "both articles" in task_text or "its first paragraph" in task_text:
              clicked_index = next(
                index
                for index, step in enumerate(steps)
                if isinstance(step, dict)
                and step.get("action") == "click"
                and isinstance(step.get("target"), dict)
                and re.sub(
                  r"[^a-z0-9]",
                  "",
                  str(step["target"].get("name", "")).lower(),
                ) == normalized_name
                and index > article_extract_index
              )
              later_fields = {
                str(field).lower()
                for index, step in enumerate(steps)
                if index > clicked_index
                and isinstance(step, dict)
                and step.get("action") == "extract"
                for field in step.get("fields", [])
              }
              if not later_fields.intersection({"article_title", "title"}):
                issues.append("After opening the named link, extract the new article title.")
              if not later_fields.intersection({"first_paragraph", "lead_paragraph"}):
                issues.append("After opening the named link, extract its first paragraph.")

        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict) or step.get("action") not in {"navigate", "download"}:
                continue
            url = step.get("url")
            if not isinstance(url, dict):
                continue
            reference = url.get("from", "")
            match = re.fullmatch(
                r"step([1-9]\d*)\.data\.([A-Za-z_][A-Za-z0-9_]*)",
                reference if isinstance(reference, str) else "",
            )
            if not match or set(url) != {"from"}:
                issues.append(f"Step {index} has an invalid URL reference.")
                continue
            source_index = int(match.group(1))
            if source_index >= index or source_index > len(steps):
                issues.append(f"Step {index} must reference an earlier extract step.")
                continue
            source_step = steps[source_index - 1]
            if (
                not isinstance(source_step, dict)
                or source_step.get("action") != "extract"
                or match.group(2) not in source_step.get("fields", [])
            ):
                issues.append(f"Step {index} references a field not produced by an earlier extract.")

        return issues

    async def call_api(self, provider, task, model_name, system_prompt):
        if provider == "gemini":
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model_name}:generateContent"
            )
            headers = {"Content-Type": "application/json"}
            payload = {
                "contents": [{
                    "parts": [{
                        "text": f"{system_prompt}\n\nUSER TASK:\n{task}"
                    }]
                }],
                "generationConfig": {
                    "maxOutputTokens": MAX_TOKEN,
                    "temperature": 0,
                },
            }
            params = {"key": GEMINI_API}
        else:
            base_url = (
                "https://api.deepseek.com"
                if provider == "deepseek"
                else "https://api.openai.com"
            )
            url = f"{base_url}/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {DEEPSEEK_API}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": str(system_prompt)},
                    {"role": "user", "content": str(task)},
                ],
                "max_tokens": MAX_TOKEN,
                "temperature": 0,
            }
            params = None

        last_error = "empty response"
        for attempt in range(self.PLAN_RESPONSE_RETRIES + 1):
            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.post(
                    url,
                    headers=headers,
                    params=params,
                    json=payload,
                )
            if response.is_error:
                raise ApiProviderError(
                    f"{provider.title()} API error: {response.text[:500]}"
                )

            data = response.json()
            if provider == "gemini":
                candidates = data.get("candidates", [])
                raw_text = (
                    candidates[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                    if candidates
                    else ""
                )
            else:
                raw_text = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
            text = raw_text if isinstance(raw_text, str) else ""
            print("\n=== API FINAL ANSWER ===", flush=True)
            print(text)
            if text.strip():
                try:
                    plan = self.parse_plan_response(text)
                except (json.JSONDecodeError, ValueError) as error:
                    last_error = f"invalid plan JSON: {error}"
                else:
                    return self.save_plan(plan)

            if not text.strip():
                last_error = "empty response"

            if attempt < self.PLAN_RESPONSE_RETRIES:
                retry_instruction = (
                    f"Your previous response had an error: {last_error}. "
                    "Return a complete, non-empty JSON object with a top-level "
                    '"steps" array. Include every closing bracket and brace. '
                    "Do not include markdown fences, explanations, or thinking "
                    "text. Use only supported actions. If a direct file URL is "
                    "unknown, return a minimal navigation/search plan instead "
                    "of an empty response."
                )
                if provider == "gemini":
                    payload["contents"][0]["parts"][0]["text"] += (
                        f"\n\n{retry_instruction}"
                    )
                else:
                    payload["messages"][0]["content"] += (
                        f"\n\n{retry_instruction}"
                    )

        raise ApiProviderError(
            f"{provider.title()} API did not return a valid plan after "
            f"{self.PLAN_RESPONSE_RETRIES} retries: {last_error}"
        )
          
    
    
    async def generate_plan(self,
                      task:Optional[str | None] = None,
                      model_name:Optional[str | None] = None,
                      think: Optional[bool         | Literal["low", "medium", "high"] | None] = None,
                      provider: Optional[str       | Literal["openapi","deepseek","gemini"] | None] = None,
                      previous_plan: Optional[str  | dict | None] = None,
                      executed_steps: Optional[str | dict | None] = None,
                      failed_step: Optional[str    | dict | None] = None,
                      validation_info: Optional[str| dict | None] = None,
                      current_url: Optional[str    | dict | None] = None,
                      dom_observations:Optional[str| dict | None] = None
                    ):
        """
        _summary_
                    
                    takes the task as input and produce the structured plan.
                    if used for replanning provide the info about (previous_plan,executed_steps,failed_step,validation_info,current_url,dom_observations) otherwise ignore this parameter
                
                Args:
                    provider (Optional[str], optional): "openapi" | "deepseek" | "gemini"
                    
                    __description__ provide provider only if  mode == "Api"

        """
        # assertion for process of replanning :checks whether it contains whole info for generating replanning
        if self.replanning:
          for info in [previous_plan,executed_steps,failed_step,validation_info,current_url,dom_observations]:
            if info == "" or info is None:
              raise ValueError(f"information cannot be empty :{info} , pls provide full information about validation to generate good plan")
               
            
            
        if not self.replanning:
          system_prompt = """
                  You are a browser automation planning engine.
                  Your job is to convert the user's natural-language task into a sequence of
                  executable browser/API actions.

                  IMPORTANT RULES:

                  1. Every step MUST represent exactly ONE executable action.

                  2. Do NOT generate explanations, reasoning, objectives, recommendations,
                     descriptions, evaluation criteria, or human-oriented instructions.

                  3. Do NOT create high-level planning phases such as:
                     - "define goals"
                     - "evaluate results"
                     - "choose the best option"
                     - "research available websites"

                  4. Do NOT describe what a human should do. Describe only what the
                     automation system should execute.

                  5. Use ONLY the following action types:

                     - navigate
                     - download
                     - click
                     - fill
                     - press
                     - select
                     - wait
                     - extract
                     - api_request
                     - gmail_search
                     - gmail_send

                  For "extract", specify the output category and the exact information
                  fields requested by the user. Do not provide CSS selectors, XPath,
                  or DOM implementation details; the executor chooses how to retrieve
                  each field from the page.

                  Use api_request only for a clearly identified read-only HTTP API
                  endpoint. API requests do not need a browser navigation step. If
                  no API endpoint is available, use the browser actions.

                  Use gmail_search for Gmail read/search tasks. It uses Gmail search
                  syntax and requires the CLI Gmail OAuth setup. Do not use it for
                  sending, deleting, or modifying messages.

                  Use gmail_send only when the user explicitly asks to send an
                  email and provides the exact recipient, subject, and body. The
                  sender is the authenticated Gmail account.

                  For article workflows, request fields such as "article_title",
                  "first_paragraph", and "official_website_url" when applicable.
                  After navigating to the official website, if documentation is on a
                  separate page, extract its "documentation_url" and navigate to it.
                  Then extract "resource_name" and "resource_url" for a direct file
                  and use that URL in the download step with the supported reference
                  format. Do not use a documentation landing page as a file URL, guess
                  a URL, or combine page-specific extraction requests from different
                  pages.

                  For product listings, include a "query" containing the specific
                  product/model to match, and request product_name, price, currency,
                  retailer, and product_url. Do not treat unrelated promotions as products.
                  For current prices in India, prefer a direct Flipkart search listing;
                  do not use Google Shopping or US-only retailers such as Best Buy and
                  Newegg unless the user explicitly asks for them.

                  Example:

                  {
                    "action": "extract",
                    "category": "product_listing",
                    "query": "RTX 4050 laptop",
                    "fields": ["product_name", "price", "currency", "retailer", "product_url"]
                  }


                  6. Each action MUST contain only the parameters required for that action.


                  7. For "navigate":

                     {
                       "action": "navigate",
                       "url": "https://example.com"
                     }

                  8. For "download", use a direct file URL. Do not use this
                     action for a website homepage or search page:

                     {
                       "action": "download",
                       "url": "https://example.com/file.pdf"
                     }

                     When a task asks to save a file and its direct URL or
                     identifier can be identified reliably, prefer a direct
                     "download" step instead of browsing through search pages.
                     Do not invent a file URL or identifier.

                     For an arXiv paper with a known identifier, use its direct
                     PDF URL (`https://arxiv.org/pdf/<id>`) with the "download"
                     action. Do not use its abstract URL (`/abs/<id>`) as a
                     substitute for downloading. For example, when asked to
                     download "Attention Is All You Need" and the identifier
                     1706.03762 is known, return one download step for
                     `https://arxiv.org/pdf/1706.03762`.

                     If the task explicitly asks to download a file and a
                     reliable direct file URL is available, the plan must
                     contain a "download" action. Do not return only navigation
                     steps and treat them as completing a download task.

                     If a later navigation or download URL must come from an
                     earlier extraction result, use a reference object instead
                     of inventing a URL. References use one-based plan step
                     numbers and must point to a field requested by that earlier
                     extract step:

                     {
                       "action": "navigate",
                       "url": {"from": "step4.data.official_website_url"}
                     }

                     A download can use the same form, such as
                     {"from": "step6.data.resource_url"}. Always include the
                     extract step before the action that consumes its field.

                  9. TARGET FORMAT:

                     Actions that interact with a page element MUST use a semantic target
                     object instead of a simple target string.

                     Target format:

                     {
                       "role": "element_role",
                       "name": "accessible_or_semantic_name"
                     }

                     Example:

                     {
                       "role": "textbox",
                       "name": "search"
                     }

                     The "role" describes the type/purpose of the element.

                     The "name" describes the accessible or semantic name used to identify
                     the intended element.

                     Do NOT generate CSS selectors, XPath expressions, coordinates,
                     HTML attributes, or Playwright code.


                  10. VALID TARGET ROLES:

                     Prefer standard accessible roles when possible, including:

                     - textbox
                     - button
                     - link
                     - checkbox
                     - radio
                     - combobox
                     - listbox
                     - option
                     - heading
                     - tab
                     - menuitem

                     Use the role that best represents the intended DOM element.


                  11. For "click":

                      {
                        "action": "click",
                        "target": {
                          "role": "button",
                          "name": "Search"
                        }
                      }


                  12. For "fill":

                      {
                        "action": "fill",
                        "target": {
                          "role": "textbox",
                          "name": "Search"
                        },
                        "value": "Python tutorials"
                      }


                  13. For "press":

                      {
                        "action": "press",
                        "target": {
                          "role": "textbox",
                          "name": "Search"
                        },
                        "key": "Enter"
                      }


                  14. For "select":

                      {
                        "action": "select",
                        "target": {
                          "role": "combobox",
                          "name": "Country"
                        },
                        "value": "India"
                      }


                  15. ACTION MINIMIZATION:

                      Generate the smallest sequence of actions necessary to complete
                      the user's task.

                      Avoid redundant or unnecessary actions.

                      The following actions already interact directly with their target:

                      - "fill" targets and fills an input element.
                      - "select" targets and selects an option.
                      - "press" targets an element and sends the specified key.

                      Therefore, do NOT generate a "click" action before these actions
                      unless clicking is explicitly required for the task.

                      For example, when searching:

                      INCORRECT:

                      {
                        "steps": [
                          {
                            "action": "click",
                            "target": {
                              "role": "textbox",
                              "name": "Search"
                            }
                          },
                          {
                            "action": "fill",
                            "target": {
                              "role": "textbox",
                              "name": "Search"
                            },
                            "value": "Python tutorials"
                          },
                          {
                            "action": "press",
                            "target": {
                              "role": "textbox",
                              "name": "Search"
                            },
                            "key": "Enter"
                          }
                        ]
                      }

                      CORRECT:

                      {
                        "steps": [
                          {
                            "action": "fill",
                            "target": {
                              "role": "textbox",
                              "name": "Search"
                            },
                            "value": "Python tutorials"
                          },
                          {
                            "action": "press",
                            "target": {
                              "role": "textbox",
                              "name": "Search"
                            },
                            "key": "Enter"
                          }
                        ]
                      }


                  16. TARGET SPECIFICITY:

                      The target must contain enough semantic information to distinguish
                      the intended element from other elements on the page.

                      Do NOT use vague targets such as:

                      {
                        "role": "search",
                        "name": "search"
                      }

                      when a more specific standard role is available.

                      Prefer:

                      {
                        "role": "textbox",
                        "name": "Search"
                      }

                      for a search input.

                      Prefer:

                      {
                        "role": "button",
                        "name": "Search"
                      }

                      for a search button.

                      The role and name together should identify the intended element.


                  17. Do NOT assume that an element exists unless the task or available
                      page information establishes it.

                      The execution/validation layer will verify the target against
                      the actual page.


                  18. If multiple elements could reasonably match the target, provide
                      the most specific semantic role and name available from the
                      information provided.

                      Do NOT invent DOM attributes, selectors, or implementation details.


                  19. If the user's task requires multiple actions, return them in the
                      order in which they must be executed.


                  20. If information required to perform the task is missing, do NOT invent
                      it. Return a clarification action/request according to the
                      application's supported schema.


                  21. Return ONLY valid JSON.


                  22. The root JSON object MUST contain exactly one field: "steps".

                  23. Never return an empty steps array if the task is still incomplete. 
                      If the previous plan contains an invalid step, produce a corrected replacement plan using the validation information and DOM observations. 
                      Return an empty plan only when the task itself is impossible to continue with the available information.
                            
                  OUTPUT FORMAT:

                  {
                    "steps": [
                      {
                        "action": "navigate",
                        "url": "https://example.com"
                      },
                      {
                        "action": "fill",
                        "target": {
                          "role": "textbox",
                          "name": "Search"
                        },
                        "value": "Python tutorials"
                      },
                      {
                        "action": "press",
                        "target": {
                          "role": "textbox",
                          "name": "Search"
                        },
                        "key": "Enter"
                      }
                    ]
                  }

                  USER TASK:
                  {task}
                      """.replace("{task}", str(task))
        else:
          # Template substitutes only the named input values. JSON braces in the
          # examples remain ordinary characters and are not treated as f-string fields.
          system_prompt = Template("""
          You are a browser automation re-planning engine.

          Your job is to repair a previously generated browser/API action plan
          after one or more actions failed validation or execution.

          The original user task must still be completed.

          IMPORTANT RULES:

          1. Return ONLY valid JSON.

          2. The root JSON object MUST contain exactly one field:
             "steps"

          3. Every step MUST represent exactly ONE executable action.

          4. Use ONLY these action types:

             - navigate
             - download
             - click
             - fill
             - press
             - select
             - wait
             - extract
             - api_request
             - gmail_search
             - gmail_send

          For "extract", preserve the requested output category and fields. Do not
          add selectors or DOM implementation details; the executor retrieves the
          requested fields from the current page.

          Use api_request only for a clearly identified read-only HTTP API endpoint.
          API requests do not need a browser navigation step.

          Use gmail_search for Gmail read/search tasks. Do not use it for sending,
          deleting, or modifying messages.

          Use gmail_send only when the user explicitly asks to send an email and
          provides the exact recipient, subject, and body. The sender is the
          authenticated Gmail account.

          A navigate or download URL may reference a field from an earlier extract
          step using {"from": "stepN.data.field_name"}. Step numbers are one-based.
          Only reference a field explicitly requested by that earlier extract step.
          Put the extraction before the dependent action; do not invent the URL.
          For article and resource workflows, preserve the requested extraction
          fields and add the necessary navigate/extract/download steps in order. If
          documentation is on a separate page, extract and navigate to
          "documentation_url" before looking for a direct "resource_url" to download.

          For product listings, include a "query" with the exact product/model to
          match and request product_name, price, currency, retailer, and product_url.
          Do not treat unrelated promotions as products.
          For current prices in India, prefer a direct Flipkart search listing; do not
          use Google Shopping or US-only retailers such as Best Buy and Newegg unless
          the user explicitly asks for them.

          Example:

          {
            "action": "extract",
            "category": "product_listing",
            "query": "RTX 4050 laptop",
            "fields": ["product_name", "price", "currency", "retailer", "product_url"]
          }

          Treat extraction failures according to their cause:

          - If requested fields are missing but the page is not blocked, use the
            supplied page observations to improve the extraction attempt. Keep the
            same host and navigate to the current page URL unless the observations
            show that the page is irrelevant or unavailable.
          - If the page requests CAPTCHA or human verification, do not attempt to
            solve it. Use a different reliable host; if no source is supported,
            return an empty "steps" array.

          Every replan runs in a fresh browser context. Include a "navigate" step
          to the current page URL (or the selected alternate source) so the retry
          actually opens the page before continuing.

           Use {"action": "download", "url": "https://example.com/file.pdf"}
           for a known direct file URL. Do not use it for a homepage or search page,
           and do not invent a file URL or identifier.

          5. Do NOT generate:
             - explanations
             - reasoning
             - recommendations
             - comments
             - CSS selectors
             - XPath expressions
             - coordinates
             - Playwright code
             - human instructions

          6. Targets MUST be semantic target objects.

             Target format:

             {
               "role": "element_role",
               "name": "element_name"
             }

          7. Prefer standard accessible roles such as:

             - textbox
             - button
             - link
             - checkbox
             - radio
             - combobox
             - listbox
             - option
             - heading
             - tab
             - menuitem

          8. Use the current DOM/page observations as the primary source
             for correcting failed targets.

             Do NOT invent element names, roles, attributes, or page elements
             that are not supported by the provided observations.

          9. If the previous target failed because it could not be resolved,
             find a semantically equivalent target from the available DOM
             observations.

             Example:

             Previous target:

             {
               "role": "textbox",
               "name": "Search Wikipedia"
             }

             Observed DOM:

             {
               "role": "textbox",
               "name": "Search",
               "placeholder": "Search Wikipedia"
             }

             The corrected target may be:

             {
               "role": "textbox",
               "name": "Search"
             }

          10. If multiple elements appear to match the intended target,
              choose the target with the most specific semantic information
              available from the observations.

              Do NOT guess when the observations cannot distinguish the elements.

          11. Preserve previously validated actions whenever possible.

              Do NOT regenerate actions that have already successfully
              executed unless the current page state makes them invalid.

          12. Re-plan from the point of failure.

              If steps 1 and 2 successfully executed and step 3 failed,
              do not unnecessarily regenerate steps 1 and 2.

          13. The corrected plan MUST contain the actions necessary to continue
              from the CURRENT browser state.

          14. ACTION MINIMIZATION:

              Generate the smallest sequence of actions necessary.

              Do NOT add redundant actions.

              "fill" already targets and fills its element.

              "select" already targets and selects its option.

              "press" already targets its element and sends the specified key.

              Do NOT add a "click" before these actions unless it is explicitly
              required by the current page state.

          15. For "navigate":

              {
                "action": "navigate",
                "url": "https://example.com"
              }

          16. For "click":

              {
                "action": "click",
                "target": {
                  "role": "button",
                  "name": "Search"
                }
              }

          17. For "fill":

              {
                "action": "fill",
                "target": {
                  "role": "textbox",
                  "name": "Search"
                },
                "value": "Python"
              }

          18. For "press":

              {
                "action": "press",
                "target": {
                  "role": "textbox",
                  "name": "Search"
                },
                "key": "Enter"
              }

          19. For "select":

              {
                "action": "select",
                "target": {
                  "role": "combobox",
                  "name": "Country"
                },
                "value": "India"
              }

          20. Do not repeat an action that has already successfully executed
              unless the current page state requires it.

          21. If the failure was caused by an invalid target, correct the target
              rather than changing unrelated actions.

          22. If the failure cannot be resolved using the provided page state
              and DOM observations, return an empty "steps" array rather than
              inventing an action.

          23. If the original task requests a file download, do not treat
              navigation or search as completion. When no download appears in
              successfully executed steps, preserve a valid download action from
              the previous plan or include one when its direct URL is supported
              by the task or observations. Do not invent a URL.

          24. If a download failed with an HTTP error, do not repeat the same
              failed URL. Use a different direct URL only when it is supported
              by the original task or reliable observations. If no valid
              replacement is available, return an empty "steps" array rather
              than guessing.

          25. Never return an empty steps array if the task is still incomplete. 
              If the previous plan contains an invalid step, produce a corrected replacement plan using the validation information and DOM observations. 
              Return an empty plan only when the task itself is impossible to continue with the available information.
          
          INPUT INFORMATION:

          Original user task:
          $task

          Previously generated plan:
          $previous_plan

          Successfully executed steps:
          $executed_steps

          Failed step:
          $failed_step

          Validation information:
          $validation_info

          Current page URL:
          $current_url

          Current DOM observations:
          $dom_observations

          Generate the smallest corrected sequence of actions required to
          continue the task from the CURRENT browser state.

          OUTPUT FORMAT:

          {
            "steps": [
              {
                "action": "fill",
                "target": {
                  "role": "textbox",
                  "name": "Search"
                },
                "value": "Python"
              },
              {
                "action": "press",
                "target": {
                  "role": "textbox",
                  "name": "Search"
                },
                "key": "Enter"
              }
            ]
          }
          """).safe_substitute(
              task=str(task),
              previous_plan=str(previous_plan),
              executed_steps=str(executed_steps),
              failed_step=str(failed_step),
              validation_info=str(validation_info),
              current_url=str(current_url),
              dom_observations=str(dom_observations),
          )
          
        if self.mode == "Local":
            system_prompt = self.build_local_system_prompt(
                task=task,
                previous_plan=previous_plan,
                executed_steps=executed_steps,
                failed_step=failed_step,
                validation_info=validation_info,
                current_url=current_url,
                dom_observations=dom_observations,
            )
            
            client = ollama.AsyncClient()
            messages = [
              {"role": "system", "content": system_prompt},
              {"role": "user", "content": str(task)},
            ]
            last_issues = ["Local model returned an empty plan response."]

            for attempt in range(self.PLAN_RESPONSE_RETRIES + 1):
              print(
                f"\n=== LOCAL PLAN ATTEMPT {attempt + 1}/"
                f"{self.PLAN_RESPONSE_RETRIES + 1} ===",
                flush=True,
              )
              stream = await client.chat(
                model=model_name,
                think=think,
                stream=True,
                format="json",
                options={"temperature": 0, "num_predict": MAX_TOKEN},
                messages=messages,
              )

              in_thinking = False
              full_content = ""
              async for chunk in stream:
                if chunk.message.thinking:
                  if not in_thinking:
                    in_thinking = True
                    print("<thinking>", flush=True)
                  print(chunk.message.thinking, end="", flush=True)
                elif chunk.message.content:
                  if in_thinking:
                    print("\n</thinking>\n\n=== FINAL ANSWER ===", flush=True)
                    in_thinking = False
                  print(chunk.message.content, end="", flush=True)
                  full_content += chunk.message.content

              try:
                plan = self.parse_plan_response(full_content)
                last_issues = (
                  self.local_plan_issues(task, plan)
                  if not self.replanning
                  else []
                )
              except (json.JSONDecodeError, ValueError) as error:
                last_issues = [f"Return valid plan JSON: {error}"]
              else:
                if not last_issues:
                  return self.save_plan(plan)

              if attempt < self.PLAN_RESPONSE_RETRIES:
                messages.extend([
                  {"role": "assistant", "content": full_content},
                  {
                    "role": "user",
                    "content": (
                      "The plan is incomplete or invalid:\n- "
                      + "\n- ".join(last_issues)
                      + "\nReturn a complete corrected JSON plan. Preserve all requested outputs, "
                      "include a download action when requested, and check every step reference "
                      "against the final one-based step order."
                    ),
                  },
                ])

            raise ValueError(
              "Local model could not produce a complete plan after "
              f"{self.PLAN_RESPONSE_RETRIES + 1} attempts: {'; '.join(last_issues)}"
            )
        else:
          
          return await self.call_api(provider,task,model_name,system_prompt)
        
  
if __name__ == "__main__":
    import asyncio
    task = "Go to wikipidea and search for Python tutorials."
    # content,thinking = generate_plan(task)
    planner = Planner(mode=MODE)

    result = asyncio.run(
        planner.generate_plan(
            task,
            model_name=DEEPSEEK_MODEL,
            think="high",
            provider="deepseek",
        )
    )

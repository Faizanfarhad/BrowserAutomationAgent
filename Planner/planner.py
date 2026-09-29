import ollama
from typing import Optional,Literal
from config import * 
import dotenv 
import os 
import httpx
import json
import asyncio
from string import Template
from pathlib import Path

dotenv.load_dotenv()

DEEPSEEK_API = os.getenv("DEEPSEEK_API")
OPENAPI = os.getenv("OPENAPI")
GEMINI_API = os.getenv("GEMINI_API")

class ApiProviderError(Exception):
    """Raised when an external model provider cannot complete the request."""

class Planner:
    PLAN_RESPONSE_RETRIES = 2

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
        if any(not isinstance(step, dict) for step in plan["steps"]):
            raise ValueError('Every item in the plan "steps" list must be an object')
        return plan

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
            
            client = ollama.AsyncClient()
            stream = await client.chat(
                    model=model_name, # i used local model 
                    think=think,  # using thinking mode 
                    stream=True,   #  Used stream true because i want to show the thinking process 
                    format="json",  # gives output into  json format 
                    messages=[
                        {"role": "system", 
                         "content":str(system_prompt)},
                        {"role": "user",
                         "content": str(f'{task}')},
                    ]
                )
                
            in_thinking = False
            full_thinking = ""
            full_content = ""
                
            async for chunk in stream:
                # Check for thinking tokens
                if chunk.message.thinking:
                    if not in_thinking:
                        in_thinking = True
                        print("<thinking>", flush=True)
                    print(chunk.message.thinking, end='', flush=True)
                    full_thinking += chunk.message.thinking
                
                # Check for final answer tokens
                elif chunk.message.content:
                    if in_thinking:
                        print("\n</thinking>\n", flush=True)
                        print("\n=== FINAL ANSWER ===", flush=True)
                        in_thinking = False
                    print(chunk.message.content, end='', flush=True)
                    full_content += chunk.message.content
            
            if full_content.strip():
              return self.save_plan(full_content)
            else:
              raise ValueError("Local model returned an empty plan response")
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

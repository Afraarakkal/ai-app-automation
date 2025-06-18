import time
import os
import json
import io
import re
from PIL import Image

# Appium Imports
from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from dotenv import load_dotenv 

# Google Gemini Imports
import google.generativeai as genai

# Load environment variables (e.g., GOOGLE_API_KEY)
load_dotenv() 

# Configure your Gemini API key
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))

# --- Appium Capabilities ---
APPIUM_SERVER_URL = 'http://localhost:4723'

# Default to ApiDemos for initial testing
API_DEMOS_PACKAGE = 'io.appium.android.apis'
API_DEMOS_ACTIVITY = 'io.appium.android.apis.ApiDemos'

# You might need to update these for your specific emulator/device
DEVICE_NAME = 'emulator-5554' # e.g., 'Pixel_4_API_30'
PLATFORM_VERSION = '15'       # e.g., '30' for Android 11, '15' for Android 4.0.3

# Initial capabilities to launch ApiDemos
capabilities = {
    'platformName': 'Android',
    'appium:platformVersion': PLATFORM_VERSION,
    'appium:deviceName': DEVICE_NAME,
    'appium:appPackage': API_DEMOS_PACKAGE,
    'appium:appActivity': API_DEMOS_ACTIVITY,
    'appium:automationName': 'UiAutomator2',
    'appium:newCommandTimeout': 300,
    'appium:noReset': True # Keep app data between sessions
}

# Global driver and wait objects (will be initialized in run_agentic_automation_with_gemini)
driver = None
wait = None

# --- Gemini Agent: The Cognitive Core ---
# ... (rest of the code) ...

class GeminiAgent:
    def __init__(self, model_name="gemini-1.5-flash"):
        self.model = genai.GenerativeModel(model_name=model_name)
        self.chat_history = [] 

        self.system_instruction = (
            "You are an AI agent controlling an Android mobile device via Appium. "
            "Your objective is to achieve the user's high-level goals by interacting with the UI. "
            "For each turn, you will receive:\n"
            "1.  The overall **User Goal** (this is the persistent goal).\n"
            "2.  A **Screenshot** of the current mobile screen.\n"
            "3.  The **UI Tree (XML structure)**, which provides precise details like resource-ids, content-descriptions, texts, and element types, even for elements not immediately visible in the screenshot. Use this to find accurate and robust locators.\n"
            "4.  (Optional) **Previous Action Outcome**: Information about the success or failure of your last suggested action.\n\n"
            "Your task is to analyze the user goal, the visual screenshot, and the structured UI XML, then decide the single best next Appium action to take. "
            "Think step-by-step to explain your reasoning before providing the JSON action. If you need to scroll to find an element, output a 'scroll' action first.\n"
            "Output your action as a single JSON object. DO NOT include any text outside the JSON block. "
            "If the goal is achieved, state 'GOAL_ACHIEVED'. If the goal is impossible, state 'GOAL_IMPOSSIBLE'.\n\n"

            "**CRITICAL: Conditional Interaction and Verification:**\n"
            "When a goal involves ensuring elements are in a specific state (e.g., 'checked', 'ON'), you MUST:\n"
            "1.  **Perceive Current State:** Identify the target element(s) by their unique text labels, IDs, or other attributes. Examine the `checked` attribute of the identified element(s) in the UI Tree XML.\n"
            "2.  **Act Conditionally:**\n"
            "    * If the element is already in the **desired state** (e.g., `checked=\"true\"` for an 'ON' goal), DO NOT click it. State this in your thought and proceed to the next unfulfilled part of the goal.\n"
            "    * If the element is **NOT** in the desired state (e.g., `checked=\"false\"` for an 'ON' goal), then perform a `click` action on *that specific element*.\n"
            "3.  **Verify All before completion:** For goals like \"ensure all X are Y\", you must confirm that *every single* 'X' element specified in the user goal is in state 'Y' by re-inspecting the UI tree. Only output 'GOAL_ACHIEVED' when you have *confirmed* all specified conditions are met.\n"
            "    * If you've clicked an element and the UI tree for the next turn *still* shows it in the incorrect state, try clicking it again or re-evaluating.\n"
            "    * If no actions are possible and the desired state is still incorrect after multiple attempts, then declare GOAL_IMPOSSIBLE.\n\n"

            "**Crucial Navigation Hint:** When the user goal mentions an item 'in the main menu' or 'within the app', prioritize finding that item by its visible **text** or `content-desc` within the *current application's active window*. Avoid navigating to system applications like 'Settings' unless explicitly instructed.\n\n"
            "**Crucial Scrolling Logic:** If your goal requires an element that is not visible on the current screen (as indicated by the UI Tree), you MUST issue a 'scroll' action in the necessary direction. If, after a scroll, the element is *still not visible*, or if a click action on the element just after a scroll failed (as indicated by 'Previous Action Outcome'), you must issue *another* 'scroll' action in the same direction. Continue scrolling iteratively until the target element becomes visible in the UI Tree, or until scrolling no longer changes the screen content (meaning you've reached the end of the scrollable area). Only attempt to 'click' an element when you have confirmed its presence in the UI Tree XML.\n\n"
            "**Allowed JSON action formats:**\n"
            "1.  **Click Element:** `{\"action\": \"click\", \"by\": \"<AppiumBy_strategy>\", \"value\": \"<locator_value>\", \"thought\": \"<reasoning>\"}`\n"
            "    (AppiumBy_strategy can be ID, ACCESSIBILITY_ID, XPATH, CLASS_NAME. Prioritize ID/ACCESSIBILITY_ID from XML. When navigating main menus within an app (e.g., ApiDemos), using **XPATH by text** is often robust for list items. If not unique, use XPATH by text or content-desc. If coordinates are only option, use 'COORDINATES' for 'by' and '[x,y]' for 'value'.)\n"
            "2.  **Type Text:** `{\"action\": \"type\", \"text\": \"<text_to_type>\", \"by\": \"<AppiumBy_strategy>\", \"value\": \"<locator_value>\", \"thought\": \"<reasoning>\"}`\n"
            "    (Target should be an input field. Use ID, ACCESSIBILITY_ID, or XPATH. Remember to press ENTER (keycode 66) after typing if necessary.)\n"
            "3.  **Scroll:** `{\"action\": \"scroll\", \"direction\": \"<up|down|left|right>\", \"thought\": \"<reasoning to scroll. If a previous scroll didn't reveal the element, explain why another scroll is needed and why you haven't reached the end yet.>\"}`\n"
            "    (Only scroll if necessary to reveal an element for the next step. Ensure you describe why you need to scroll.)\n"
            "4.  **Press Keycode:** `{\"action\": \"press_keycode\", \"key_code\": <android_keycode_int>, \"thought\": \"<reasoning>\"}`\n"
            "    (e.g., 4 for BACK, 66 for ENTER/Done on keyboard. Only use when explicitly needed for navigation or keyboard dismissal.)\n"
            "5.  **Launch App:** `{\"action\": \"launch_app\", \"package\": \"<app_package>\", \"activity\": \"<app_activity>\", \"thought\": \"<reasoning>\"}`\n"
            "6.  **Terminate App:** `{\"action\": \"terminate_app\", \"package\": \"<app_package>\", \"thought\": \"<reasoning>\"}`\n"
            "7.  **GOAL_ACHIEVED:** `{\"action\": \"GOAL_ACHIEVED\", \"thought\": \"The user's goal has been successfully completed based on current screen analysis.\"}`\n"
            "8.  **GOAL_IMPOSSIBLE:** `{\"action\": \"GOAL_IMPOSSIBLE\", \"thought\": \"I cannot achieve this goal given the current UI state or the constraints, or an unrecoverable error occurred.\"}`\n"
            "Always provide a valid JSON object. Do not include extra text outside the JSON. Be precise with AppiumBy_strategy and locator_value by consulting the UI Tree XML."
            "Here's an example for typing into the 'Custom Title' text field:\n"
            "Example Scenario: You are on the Custom Title screen and need to type into the text field.\n"
            "Screenshot: [image of custom title screen]\n"
            "UI Tree (relevant part for custom title text field):\n"
            "```xml\n"
            "<node class=\"android.widget.EditText\" resource-id=\"android:id/edit\" text=\"\" content-desc=\"Left is best\" />\n"
            "```\n"
            "Expected Action: ```json\n"
            "{\"action\": \"type\", \"text\": \"My New Title\", \"by\": \"ID\", \"value\": \"android:id/edit\", \"thought\": \"Identified the editable text field by its unique resource-id to type the new title.\"}\n"
            "```\n"
        )

    # ... (rest of the GeminiAgent class, AppiumExecutor class, and run_agentic_automation_with_gemini function remain the same) ...

    def _prepare_image_for_gemini(self, screenshot_binary):
        """Converts raw screenshot binary to a PIL Image object for Gemini."""
        try:
            img_buffer = io.BytesIO(screenshot_binary)
            img = Image.open(img_buffer)
            return img
        except Exception as e:
            print(f"[Image Preprocessing ERROR]: Could not prepare image for Gemini: {e}")
            raise

    def analyze_and_plan(self, goal_text, screenshot_binary, ui_tree_xml, prev_action_outcome=None):
        """
        Sends the goal, screenshot, UI tree, and previous action outcome to Gemini to get the next action.
        """
        image_part = self._prepare_image_for_gemini(screenshot_binary)

        # Construct the prompt payload for Gemini
        prompt_parts = [
            self.system_instruction,
            f"\n\nUser Goal: {goal_text}",
            "\n\nCurrent Mobile Screen (Screenshot):",
            image_part, # PIL Image object directly
            "\n\nUI Tree (XML Structure - for precise element attributes):",
            f"\n```xml\n{ui_tree_xml}\n```",
        ]
        if prev_action_outcome: # Add previous action outcome if available
            prompt_parts.append(f"\n\nPrevious Action Outcome: {prev_action_outcome}")

        prompt_parts.append("\n\nWhat is the next action (JSON format)? Provide a precise action with reasoning.")

        try:
            # Send the request to Gemini, maintaining conversation history for context
            convo = self.model.start_chat(history=self.chat_history)
            response = convo.send_message(prompt_parts)

            # Update chat history for the next turn
            self.chat_history = convo.history

            # Extract the action from Gemini's response
            response_text = response.text.strip()
            print(f"\n[Gemini Agent Raw Response]:\n{response_text}")

            # Robustly parse the JSON response from Gemini
            json_match = re.search(r"```json\s*(\{.*?\})\s*```", response_text, re.DOTALL)
            if json_match:
                action_json_str = json_match.group(1)
            else:
                # Fallback: if no code block, try to parse the entire response as JSON
                action_json_str = response_text

            action = json.loads(action_json_str)
            return action

        except json.JSONDecodeError as e:
            print(f"[Gemini Agent ERROR]: Could not parse JSON from Gemini response: {e}")
            print(f"Raw response was: {response_text}")
            return {"action": "ERROR", "message": "JSON_PARSE_ERROR", "raw_response": response_text}
        except Exception as e:
            print(f"[Gemini Agent ERROR]: Error calling Gemini API or processing response: {e}")
            return {"action": "ERROR", "message": str(e), "raw_response": response_text}

# --- Appium Executor (Translates Gemini's action to Appium commands) ---
class AppiumExecutor:
    def __init__(self, driver_instance):
        self.driver = driver_instance
        self.wait = WebDriverWait(driver_instance, 30) # Use the passed driver instance

    def execute_action(self, action):
        action_type = action.get("action")
        thought = action.get("thought", "No specific thought provided.")
        print(f"\n[Executor]: Executing action '{action_type}' (Thought: {thought})")

        try:
            if action_type == "click":
                locator_by_str = action["by"].upper()
                locator_value = action["value"]
                if locator_by_str == "COORDINATES": # Special case for coordinates
                    x, y = locator_value[0], locator_value[1]
                    self.driver.tap([(x, y)])
                    print(f"   -> Tapped coordinates: ({x}, {y})")
                else:
                    locator_by = getattr(AppiumBy, locator_by_str)
                    # Use presence_of_element_located for more robustness against clickable issues
                    element = self.wait.until(EC.presence_of_element_located((locator_by, locator_value))) 
                    element.click()
                    print(f"   -> Clicked element: By={locator_by_str}, Value='{locator_value}'")
                return {"status": "success"}

            elif action_type == "type":
                locator_by_str = action["by"].upper()
                locator_value = action["value"]
                text_to_type = action["text"]
                locator_by = getattr(AppiumBy, locator_by_str)
                element = self.wait.until(EC.presence_of_element_located((locator_by, locator_value)))
                element.send_keys(text_to_type)
                self.driver.press_keycode(66) # Press Enter
                print(f"   -> Typed '{text_to_type}' into element: By={locator_by_str}, Value='{locator_value}'. Pressed Enter.")
                return {"status": "success"}

            elif action_type == "scroll":
                direction = action["direction"].lower()
                screen_size = self.driver.get_window_size()
                
                # Execute the scroll gesture
                self.driver.execute_script("mobile: scrollGesture", {
                    "left": screen_size['width'] * 0.1, "top": screen_size['height'] * 0.1,
                    "width": screen_size['width'] * 0.8, "height": screen_size['height'] * 0.8,
                    "direction": direction,
                    "percent": 0.8 # How much of the scrollable area to scroll
                })
                print(f"   -> Scrolled {direction}.")
                return {"status": "success"}

            elif action_type == "press_keycode":
                key_code = action["key_code"]
                self.driver.press_keycode(key_code)
                print(f"   -> Pressed Android keycode: {key_code}")
                return {"status": "success"}

            elif action_type == "launch_app":
                app_package = action["package"]
                self.driver.activate_app(app_package)
                print(f"   -> Launched/activated app: {app_package}")
                return {"status": "success"}

            elif action_type == "terminate_app":
                app_package = action["package"]
                self.driver.terminate_app(app_package)
                print(f"   -> Terminated app: {app_package}")
                return {"status": "success"}

            elif action_type == "GOAL_ACHIEVED":
                print("   -> Goal achieved as per Gemini. Signalling completion.")
                return {"status": "goal_achieved"}
            elif action_type == "GOAL_IMPOSSIBLE":
                print("   -> Goal deemed impossible by Gemini. Signalling failure.")
                return {"status": "goal_impossible"}
            elif action_type == "ERROR":
                print(f"   -> Gemini returned an error action: {action.get('message')}. Raw response: {action.get('raw_response')}")
                return {"status": "gemini_error", "message": action.get('message'), "raw_response": action.get('raw_response')}
            else:
                print(f"   -> Unknown action type returned by Gemini: '{action_type}'. Stopping.")
                return {"status": "unknown_action", "action_type": action_type}

        except (TimeoutException, NoSuchElementException) as e:
            print(f"[Executor ERROR]: Element not found or timed out for action '{action_type}'.")
            print(f"   Details: {e}")
            self.driver.save_screenshot("execution_error_element_not_found.png")
            return {"status": "failed", "reason": "element_not_found", "details": str(e)}
        except WebDriverException as e:
            print(f"[Executor ERROR]: Appium WebDriver error for action '{action_type}'.")
            print(f"   Details: {e}")
            self.driver.save_screenshot("execution_error_webdriver.png")
            return {"status": "failed", "reason": "webdriver_error", "details": str(e)}
        except Exception as e:
            print(f"[Executor ERROR]: An unexpected error occurred during action '{action_type}'.")
            print(f"   Details: {e}")
            self.driver.save_screenshot("execution_error_general.png")
            return {"status": "failed", "reason": "general_error", "details": str(e)}
        finally:
            time.sleep(2)


# --- Main Agentic Automation Loop ---
def run_agentic_automation_with_gemini(user_goal):
    global driver, wait # Ensure we operate on the global driver and wait objects

    try:
        appium_options = UiAutomator2Options().load_capabilities(capabilities)
        print("Starting Appium session...")
        driver = webdriver.Remote(APPIUM_SERVER_URL, options=appium_options)
        print("Appium session started successfully!")
        wait = WebDriverWait(driver, 30) # Initialize WebDriverWait here

        gemini_agent = GeminiAgent()
        appium_executor = AppiumExecutor(driver) # Pass the initialized driver

        max_turns = 20 # Increased max turns for more attempts, especially with scrolling
        final_status = "Unknown"
        prev_action_outcome = None # To pass feedback to Gemini

        for turn in range(max_turns):
            print(f"\n--- Agent Turn {turn + 1}/{max_turns} ---")

            # 1. Perception: Get current screen state
            try:
                screenshot_binary = driver.get_screenshot_as_png()
                ui_tree_xml = driver.page_source
                print("[Orchestrator]: Captured current screen state (screenshot + UI tree XML).")
            except WebDriverException as e:
                print(f"[Orchestrator ERROR]: Failed to capture screen or page source: {e}. Cannot proceed.")
                final_status = "Perception_Failed"
                break

            # 2. Planning: Ask Gemini for the next action
            print("[Orchestrator]: Consulting Gemini for the next action based on goal, screen, and UI tree...")
            action = gemini_agent.analyze_and_plan(user_goal, screenshot_binary, ui_tree_xml, prev_action_outcome)

            # Reset prev_action_outcome for the next turn unless it's explicitly set by a failure below
            prev_action_outcome = None 

            # Check for immediate termination signals from Gemini
            if action.get("action") == "GOAL_ACHIEVED":
                final_status = "Goal_Achieved"
                print(f"\n[Agent]: Goal '{user_goal}' achieved after {turn + 1} turns!")
                break
            elif action.get("action") == "GOAL_IMPOSSIBLE":
                final_status = "Goal_Impossible"
                print(f"\n[Agent]: Goal '{user_goal}' deemed impossible after {turn + 1} turns. Stopping.")
                break
            elif action.get("action") == "ERROR":
                final_status = f"Gemini_Error: {action.get('message')}"
                print(f"\n[Agent]: Error from Gemini. Stopping. Raw response: {action.get('raw_response')}")
                break
            
            # 3. Execution: Perform the action
            execution_result = appium_executor.execute_action(action)

            if execution_result["status"] == "failed":
                # Provide feedback to Gemini for the next turn
                prev_action_outcome = (
                    f"Last action '{action.get('action')}' "
                    f"with value '{action.get('value', 'N/A')}' "
                    f"failed because: {execution_result['reason']}. "
                    f"Details: {execution_result['details']}. "
                    f"Please re-evaluate the current screen and plan the next step."
                )
                print(f"\n[Agent]: Action failed during execution. Current plan may be invalid. Will re-plan.")
                # DO NOT BREAK here; allow the loop to continue and feed the failure to Gemini
            elif execution_result["status"] == "goal_achieved":
                final_status = "Goal_Achieved"
                print(f"\n[Agent]: Goal '{user_goal}' achieved after {turn + 1} turns!")
                break
            elif execution_result["status"] == "goal_impossible":
                final_status = "Goal_Impossible"
                print(f"\n[Agent]: Goal '{user_goal}' deemed impossible after {turn + 1} turns. Stopping.")
                break
            # If status is "success" or "unknown_action", prev_action_outcome remains None and the loop continues

            if turn == max_turns - 1:
                final_status = "Max_Turns_Reached"
                print(f"\n[Agent]: Reached maximum allowed turns ({max_turns}) without achieving the goal. Stopping.")

    except Exception as e:
        final_status = f"Overall_System_Error: {e}"
        print(f"\n[Agentic Automation ERROR]: An error occurred during the main agent loop: {e}")
        if driver:
            screenshot_path = "agent_overall_failure.png"
            driver.save_screenshot(screenshot_path)
            print(f"Screenshot saved to {os.path.abspath(screenshot_path)}")
    finally:
        if driver: # Ensure driver was successfully initialized before quitting
            print(f"\n--- Automation Finished. Status: {final_status} ---")
            print("Quitting Appium session...")
            driver.quit()
            print("Appium session closed.")

# --- How to run the Agentic Automation ---
if __name__ == "__main__":
    if not os.getenv("GOOGLE_API_KEY"):
        print("ERROR: GOOGLE_API_KEY environment variable not set.")
        print("Please set it before running the script (e.g., export GOOGLE_API_KEY='your_api_key_here').")
        exit()

    # --- Updated goal to include typing ---
    user_goal_scrolling = """
Find 'Preference' in the main menu.
Click '9. Switch'.
On the 'Preference/9. Switch' screen:
1.  Verify the 'Checkbox preference'. If it is unchecked, click it to check it.
2.  Verify the 'Switch preference' (the one without 'custom text'). If it is OFF, click it to turn it ON.
3.  Verify the 'Switch preference\\nThis is a switch with custom text'. If it is OFF, click it to turn it ON.
Only declare GOAL_ACHIEVED after you have confirmed all three elements (Checkbox, Switch 1, Switch 2) are in their respective checked/ON states.
"""
    print(f"Attempting to achieve goal: '{user_goal_scrolling}'")
    run_agentic_automation_with_gemini(user_goal_scrolling)
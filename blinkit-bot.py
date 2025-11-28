import os
import re
import datetime
import json
from PIL import Image
import io
import time # Added time import for sleep
from zoneinfo import ZoneInfo
from google.cloud import vision
from google.oauth2 import service_account
from google.api_core.exceptions import ResourceExhausted 
import google.generativeai as genai
from telegram import Bot, Update
from telegram.ext import Updater, MessageHandler, Filters, CommandHandler
from telegram.error import BadRequest # Explicitly import BadRequest for clarity
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# === CONFIGURATION ===
BLINKIT_BOT_TOKEN = os.getenv("BLINKIT_BOT_TOKEN")  # Add this to your environment variables

INDIA_TZ = ZoneInfo("Asia/Kolkata")

# Google Sheets configuration
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
CREDS_FILE = "service_account.json"
ALLOWANCE_SHEET_ID = "1lQYE49QXPw4al7rSZMnaMKUytGckYYd85nico-D_weE"
TAB_NAME_ALLOWANCE = "Blinkit Transactions jatin"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY1")  # Try both env vars

# === Bot Initialization ===
updater = Updater(token=BLINKIT_BOT_TOKEN, use_context=True)
dispatcher = updater.dispatcher

# === Global Google Sheets Client ===
try:
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, SCOPE)
    client = gspread.authorize(creds)
    print("Google Sheets client initialized successfully")
except Exception as e:
    print(f"Failed to initialize Google Sheets client: {e}")
    raise

# === Google Vision API Setup ===
try:
    vision_creds = service_account.Credentials.from_service_account_file(CREDS_FILE)
    vision_client = vision.ImageAnnotatorClient(credentials=vision_creds)
    print("Google Vision API client initialized successfully")
except Exception as e:
    print(f"Warning: Google Vision API not initialized: {e}")
    vision_client = None

# === Google Gemini AI Setup (UPDATED to 2.5) ===
try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        # Using the latest stable and recommended model
        gemini_model = genai.GenerativeModel('gemini-2.5-flash')
        print("Google Gemini AI initialized successfully (using gemini-2.5-flash)")
    else:
        print("Warning: GEMINI_API_KEY not found. AI parsing will not be available.")
        gemini_model = None
except Exception as e:
    print(f"Warning: Google Gemini AI not initialized: {e}")
    gemini_model = None

# === Utility Functions for Telegram Markdown ===
def escape_markdown_v1(text):
    """
    Escapes special characters in MarkdownV1 used by Telegram to prevent parsing errors.
    Focuses on the most common breaking characters: *, _, [, ].
    """
    if text is None:
        return ""
    # Ensure text is a string
    text = str(text)
    
    return (
        text.replace('*', '\\*')
        .replace('_', '\\_')
        .replace('[', '\\[')
        .replace(']', '\\]')
    )

# === Vision API Functions ===
def extract_text_from_image(image_bytes):
    """Extract text from image using Google Vision API"""
    try:
        if vision_client is None:
            print("Vision API not initialized")
            return ""

        image = vision.Image(content=image_bytes)
        response = vision_client.text_detection(image=image)
        texts = response.text_annotations

        if texts:
            full_text = texts[0].description
            print(f"Extracted text: {full_text}")
            return full_text
        else:
            print("No text found in image")
            return ""

    except Exception as e:
        print(f"Error extracting text from image: {e}")
        import traceback
        traceback.print_exc()
        return ""

# === AI Extraction Functions ===
def extract_order_details_with_ai(image_bytes, max_retries=5):
    """
    Use Google Gemini AI to extract Blinkit order details from image
    Returns: dict with 'total_amount', 'items', 'delivery_charge', 'handling_charge'
    """
    
    # Check if the model is initialized before entering the loop
    if not gemini_model:
        print("⚠️ Gemini AI not available")
        return None

    for attempt in range(max_retries):
        try:
            print(f"\n=== AI EXTRACTION STARTED (Attempt {attempt + 1}/{max_retries}) ===")

            # Convert bytes to PIL Image
            image = Image.open(io.BytesIO(image_bytes))

            # Create prompt for Blinkit/Instamart orders with charges extraction
            prompt = """
You are analyzing a food delivery or grocery order screenshot (Blinkit, Instamart, Swiggy, etc.).

CRITICAL: Extract ONLY the information that is CLEARLY VISIBLE in the image. DO NOT guess or make up any numbers.

Please extract the following information and return it as a JSON object:

{
  "total_amount": <final total amount in rupees as a number>,
  "delivery_charge": <delivery charge in rupees as a number, 0 if free>,
  "handling_charge": <handling charge in rupees as a number, 0 if free>,
  "items": [
    {
      "name": "<item name>",
      "quantity": "<quantity with unit, e.g., '8 x 500g' or '4'>",
      "price": <final price in rupees as a number>
    }
  ]
}

STRICT Rules:
1. For total_amount: Extract the EXACT FINAL/GRAND TOTAL amount shown (not item total, MRP, or subtotal)
2. DO NOT round numbers - extract EXACTLY as shown (e.g., if it says 94, return 94, NOT 100)
3. For items: Extract ALL ordered items with their EXACT quantities and EXACT FINAL prices (after discounts)
4. For delivery_charge: Extract exact amount. If it says "FREE" or "₹0", return 0
5. For handling_charge: Extract exact amount. If it says "FREE" or "₹0", return 0
6. If quantity has units (g, kg, ml, etc.), include them exactly as shown
7. Clean up item names (remove checkmarks, extra symbols)
8. Return ONLY valid JSON, no additional text
9. If you're unsure about any number, return an error instead of guessing

If you cannot extract the information with certainty, return:
{"error": "Could not extract order details"}
"""

            # Generate content with AI
            print("Extracting with Gemini AI...")
            response = gemini_model.generate_content([prompt, image])

            print(f"AI Response received")
            print(f"Response text: {response.text[:500]}")

            # Parse JSON response
            response_text = response.text.strip()

            # Remove markdown code blocks if present
            if response_text.startswith("```json"):
                response_text = response_text.replace("```json", "").replace("```", "").strip()
            elif response_text.startswith("```"):
                response_text = response_text.replace("```", "").strip()

            result = json.loads(response_text)

            if "error" in result:
                print(f"❌ AI could not extract data: {result['error']}")
                return None

            # Validate and format result
            if "total_amount" not in result:
                print("❌ No total_amount in AI response")
                return None

            ai_amount = result["total_amount"]
            result["amount_corrected"] = False
            result["confidence"] = "high"
            result["validation_warning"] = False
            print(f"✅ Using AI amount directly: ₹{ai_amount}")

            # Ensure items list exists
            if "items" not in result:
                result["items"] = []
            
            # Ensure charges exist with default 0
            if "delivery_charge" not in result:
                result["delivery_charge"] = 0
            if "handling_charge" not in result:
                result["handling_charge"] = 0

            print(f"✅ AI Extraction completed")
            print(f"   Final Amount: ₹{result['total_amount']}")
            print(f"   Delivery Charge: ₹{result['delivery_charge']}")
            print(f"   Handling Charge: ₹{result['handling_charge']}")
            if result.get("items"):
                print(f"   Items extracted: {len(result['items'])}")

            return result

        except Exception as e:
            error_str = str(e)
            retry_delay = 30 # Default safety delay for quota limits
            
            # Use ResourceExhausted exception for reliable quota checks
            is_quota_error = isinstance(e, ResourceExhausted) or "429" in error_str
            
            if is_quota_error:
                print(f"⚠️ Rate limit/quota error on attempt {attempt + 1}")
                
                # --- Attempt to parse the exact retry delay from the error message ---
                if "retry in" in error_str.lower():
                    # Pattern: "retry in 25.506087534s"
                    match = re.search(r'retry in (\d+\.?\d*)s', error_str.lower())
                    if match:
                        retry_delay = float(match.group(1))
                    else:
                        # Pattern: "retry_delay { seconds: 25 }"
                        match = re.search(r'seconds: (\d+)', error_str)
                        if match:
                            retry_delay = int(match.group(1))
                
                if attempt < max_retries - 1:
                    # Add a little extra time to ensure the quota resets
                    wait_time = retry_delay + 2
                    print(f"⏳ Waiting {wait_time:.1f} seconds before retry...")
                    time.sleep(wait_time)
                    continue  # Retry
                else:
                    print(f"❌ Max retries reached. Quota exceeded.")
                    # Return a specific error dictionary that handle_photo can use
                    return {"error": "quota_exceeded", "retry_after": retry_delay}
            
            # For other errors (JSON parsing, connection issues, etc.)
            else:
                print(f"❌ Error in AI extraction (non-quota): {e}")
                if attempt < max_retries - 1:
                    print(f"⏳ Retrying in 5 seconds...")
                    time.sleep(5)
                    continue
                else:
                    import traceback
                    traceback.print_exc()
                    return None
    
    return None # Should not reach here

# === Google Sheets Functions ===
def generate_order_id():
    """Generate unique order ID using timestamp"""
    now = datetime.datetime.now(INDIA_TZ)
    # Format: ORD-YYYYMMDD-HHMMSS
    return f"ORD-{now.strftime('%Y%m%d-%H%M%S')}"

def save_blinkit_items(telegram_name, items, total_charges, order_date, order_time):
    """
    Save each item as a separate row in Google Sheets with same Order ID
    
    Args:
        telegram_name: Name of telegram user
        items: List of item dictionaries with name, quantity, price
        total_charges: Sum of delivery + handling charges
        order_date: Date string
        order_time: Time string
    """
    try:
        sheet = client.open_by_key(ALLOWANCE_SHEET_ID).worksheet(TAB_NAME_ALLOWANCE)

        headers = sheet.row_values(1)
        expected_headers = ["Order ID", "Date", "Time", "Telegram Name", "Item Name", 
                           "Quantity", "Price", "Charges", "Order Type"]

        if not headers or len(headers) < 9:
            sheet.update('A1:I1', [expected_headers])

        # Generate unique order ID for this screenshot
        order_id = generate_order_id()

        # Prepare all rows to insert at once (more efficient)
        rows_to_insert = []
        
        for item in items:
            item_name = item.get('name', 'Unknown Item')
            item_qty = item.get('quantity', '1')
            item_price = item.get('price', 0)
            
            try:
                item_price = float(item_price)
            except (TypeError, ValueError):
                item_price = 0.0
            
            row_data = [
                order_id,  # Same order ID for all items in this screenshot
                order_date,
                order_time,
                telegram_name,
                item_name,
                item_qty,
                item_price,
                total_charges,  # All items get the same charges
                "Blinkit"
            ]
            rows_to_insert.append(row_data)
        
        # Insert all rows at once
        if rows_to_insert:
            sheet.append_rows(rows_to_insert)
            print(f"✓ Saved {len(rows_to_insert)} items for {telegram_name}")
            print(f"  Order ID: {order_id}")
            print(f"  Total charges (delivery + handling): ₹{total_charges}")
            return True, order_id
        else:
            print(f"⚠️ No items to save for {telegram_name}")
            return False, None

    except Exception as e:
        print(f"Error saving to allowance sheet: {e}")
        import traceback
        traceback.print_exc()
        return False, None

# === Message Handlers ===
def handle_start(update: Update, context):
    """Handle /start command"""
    user_name = update.message.from_user.first_name or update.message.from_user.username or "there"

    welcome_msg = f"""
👋 Hi {user_name}!

🛒 *Blinkit/Instamart Order Bot*

📸 *Just send your order screenshot and I'll process it!*

No registration needed - just send:
• Blinkit orders
• Instamart orders  
• Any grocery delivery screenshots

The bot will automatically:
✅ Extract total amount
✅ Extract all items with prices
✅ Extract delivery & handling charges
✅ Save each item as separate row in Google Sheets

That's it! Simple. 😊

✨ Works in groups too!
"""

    update.message.reply_text(welcome_msg, parse_mode='Markdown')

def handle_photo(update: Update, context):
    """Handle photo messages - automatically process Blinkit screenshots"""
    user = update.message.from_user
    user_id = user.id
    user_first_name = user.first_name or ""
    user_username = user.username or ""
    user_display_name = user_first_name or user_username or f"User_{user_id}"
    
    chat_type = update.effective_chat.type  # 'private', 'group', or 'supergroup'

    try:
        processing_msg = update.message.reply_text("⏳ Processing your order...")

        photo = update.message.photo[-1]
        print(f"Photo from {user_display_name}, file_size: {photo.file_size}")

        # Check file size before downloading (10MB limit)
        if photo.file_size > 10 * 1024 * 1024:
            processing_msg.edit_text("❌ Image too large (max 10MB allowed).")
            return

        file = photo.get_file()
        image_bytes = file.download_as_bytearray()

        processing_msg.edit_text("⏳ Extracting order details with AI...")

        # Use AI extraction
        result = extract_order_details_with_ai(bytes(image_bytes))

        # Check for quota error specifically
        if isinstance(result, dict) and result.get("error") == "quota_exceeded":
            retry_after = result.get("retry_after", 30)
            processing_msg.edit_text(
                f"⚠️ *API Quota Exceeded*\n\n"
                f"The Gemini AI quota has been reached. This usually resets quickly on the free tier.\n\n"
                f"Please try again in about {int(retry_after)} seconds.\n\n"
                f"💡 If this persists, contact the developer to check the API plan.",
                parse_mode='Markdown'
            )
            return
        
        if not result or "total_amount" not in result:
            processing_msg.edit_text(
                "❌ Could not extract order details from the image.\n\n"
                "💡 Tips:\n"
                "• Make sure the image is clear and not blurry\n"
                "• Ensure the final total amount is clearly visible\n"
                "• Try taking the screenshot again\n\n"
                "Send a new screenshot to try again!"
            )
            return

        total_amount = result["total_amount"]
        items = result.get("items", [])
        delivery_charge = result.get("delivery_charge", 0)
        handling_charge = result.get("handling_charge", 0)
        total_charges = delivery_charge + handling_charge

        processing_msg.edit_text("⏳ Saving to sheet...")

        # Get current date and time
        now = datetime.datetime.now(INDIA_TZ)
        order_date = now.strftime("%Y-%m-%d")
        order_time = now.strftime("%H:%M:%S")

        success, order_id = save_blinkit_items(
            user_display_name,
            items,
            total_charges,
            order_date,
            order_time
        )

        if success:
            # --- Apply Markdown Escaping to User Data ---
            escaped_display_name = escape_markdown_v1(user_display_name)
            
            # Add user context in group chats
            user_tag = ""
            if chat_type in ['group', 'supergroup']:
                user_tag = f"📱 Submitted by: {escaped_display_name}\n\n"

            confirmation = [
                f"✅ Order recorded successfully!\n",
                user_tag,
                f"🆔 *Order ID: {order_id}*",
                f"💰 *Total Amount: ₹{total_amount:.2f}*",
            ]

            if total_charges > 0:
                confirmation.append(f"📦 *Charges: ₹{total_charges:.2f}* (Delivery: ₹{delivery_charge:.2f} + Handling: ₹{handling_charge:.2f})")
            else:
                confirmation.append(f"📦 *Charges: FREE* 🎉")

            if items:
                confirmation.append(f"\n🛒 *Items Saved ({len(items)} items):*")
                for item in items[:8]:
                    # --- Apply Markdown Escaping to Item Name ---
                    item_name = escape_markdown_v1(item.get('name', 'Unknown'))
                    item_qty = item.get('quantity', '1')
                    item_price = item.get('price', 0)
                    confirmation.append(f"  • {item_qty} x {item_name} - ₹{item_price:.2f}")
                if len(items) > 8:
                    confirmation.append(f"  ... and {len(items) - 8} more items")
            else:
                confirmation.append(f"\n⚠️ Note: Could not extract item details.")

            confirmation.extend([
                f"\n📅 {now.strftime('%d %b %Y')}",
                f"⏰ {now.strftime('%I:%M %p')}",
                f"\n✨ Each item saved as separate row",
                f"\nSend another screenshot to submit another order!"
            ])

            processing_msg.edit_text("\n".join(confirmation), parse_mode='Markdown')
        else:
            processing_msg.edit_text(
                "❌ Error saving to sheet. Please try again or contact admin.\n\n"
                "You can send the screenshot again to retry."
            )

    except BadRequest as e:
        # Catch the specific Telegram parsing error for cleaner output
        print(f"❌ Telegram Formatting Error: {e}")
        import traceback
        traceback.print_exc()
        try:
            update.message.reply_text(
                "❌ Error formatting the confirmation message. Please contact admin.\n\n"
                "Your order may have still been saved."
            )
        except Exception:
             pass
             
    except Exception as e:
        print(f"Error processing order: {e}")
        import traceback
        traceback.print_exc()
        # Fallback error message
        try:
            update.message.reply_text(
                "❌ Critical error processing image. Please try again or contact admin.\n\n"
                "You can send the screenshot again to retry."
            )
        except Exception:
             pass # Failsafe

def handle_text(update: Update, context):
    """Handle text messages"""
    update.message.reply_text(
        "📸 Please send a screenshot of your Blinkit/Instamart order.\n\n"
        "I can only process images/screenshots, not text messages."
    )

def setup_handlers():
    """Setup message handlers"""
    # Command handlers
    dispatcher.add_handler(CommandHandler("start", handle_start))

    # Message handlers - process photos automatically
    dispatcher.add_handler(MessageHandler(Filters.photo, handle_photo))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))

# === Main Entry Point ===
if __name__ == "__main__":
    print("Blinkit Bot starting...")
    print("Features:")
    print("- Each item saved as separate row")
    print("- Extracts delivery & handling charges")
    print("- Only captures Telegram name (no username/ID)")
    setup_handlers()
    print("Bot handlers configured")
    print("Ready to process Blinkit/Instamart order screenshots automatically.")
    print("Press Ctrl+C to stop the bot.")

    # Start polling for updates
    updater.start_polling()
    updater.idle()
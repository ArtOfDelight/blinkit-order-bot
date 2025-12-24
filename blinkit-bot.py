import os
import re
import datetime
import json
from PIL import Image
import io
import time # Added time import for sleep
import asyncio
from zoneinfo import ZoneInfo
from google.cloud import vision
from google.oauth2 import service_account
from google.api_core.exceptions import ResourceExhausted 
import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, ConversationHandler, filters
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

# === Order Type Identifier Mapping ===
ORDER_TYPE_IDENTIFIERS = {
    "sri ganeshaya namaha": "Prem Marketing",
}

# === Conversation States ===
WAITING_FOR_MANUAL_AMOUNT = 1
WAITING_FOR_PAYMENT_METHOD = 2
WAITING_FOR_MORE_PHOTOS = 3

# === Bot Initialization ===
# Application will be initialized in main
application = None

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
    Use Google Gemini AI to extract Blinkit order details from one or more images
    Args:
        image_bytes: Either a single bytes object or a list of bytes objects
    Returns: dict with 'total_amount', 'items', 'delivery_charge', 'handling_charge'
    """

    # Check if the model is initialized before entering the loop
    if not gemini_model:
        print("⚠️ Gemini AI not available")
        return None

    # Ensure we have a list (backwards compatibility with single image)
    if isinstance(image_bytes, bytes):
        image_bytes_list = [image_bytes]
    else:
        image_bytes_list = image_bytes

    for attempt in range(max_retries):
        try:
            print(f"\n=== AI EXTRACTION STARTED (Attempt {attempt + 1}/{max_retries}) ===")
            print(f"Processing {len(image_bytes_list)} screenshot(s)")

            # Convert all bytes to PIL Images
            images = [Image.open(io.BytesIO(img_bytes)) for img_bytes in image_bytes_list]

            # Create prompt for Blinkit/Instamart orders with charges extraction
            # Build identifier detection instructions
            identifier_instructions = ""
            if ORDER_TYPE_IDENTIFIERS:
                identifier_list = []
                for identifier, order_type in ORDER_TYPE_IDENTIFIERS.items():
                    identifier_list.append(f'   - If you find "{identifier}" (case-insensitive) -> set "order_type": "{order_type}"')
                identifier_instructions = "\n10. IDENTIFIER DETECTION:\n" + \
                    "   Check the FIRST FEW ROWS/LINES of the image for these text identifiers:\n" + \
                    "\n".join(identifier_list) + \
                    '\n   - If NONE found, set "order_type": "Blinkit"\n   - Match is case-insensitive'

            prompt = """
You are analyzing one or more food delivery or grocery order screenshots (Blinkit, Instamart, Swiggy, etc.).

IMPORTANT: You may receive MULTIPLE screenshots for the SAME order. This happens when:
- The items list is long and requires scrolling
- Some screenshots show the top items, others show items after scrolling down
- Some screenshots show the bill details/totals at the bottom

Each screenshot could be:
- Order confirmation screen (shows grand total at bottom)
- Order details/items screen (shows items grouped by category with "Summary" and "Items" tabs)
- Receipt/invoice screen
- Continuation of items list (scrolled down)

CRITICAL: If you receive multiple screenshots, COMBINE the information from ALL of them:
- Merge ALL items from ALL screenshots (remove duplicates if any)
- Use the bill details/total from whichever screenshot contains it
- This is ONE order split across multiple screenshots

DO NOT guess or make up any numbers. Only extract what is CLEARLY VISIBLE across all screenshots.

Please extract the following information and return it as a JSON object:

{
  "order_type": "<detected order type from identifiers or Blinkit>",
  "total_amount": <final total amount in rupees as a number>,
  "delivery_charge": <delivery charge in rupees as a number, 0 if free>,
  "handling_charge": <handling charge in rupees as a number, 0 if free>,
  "items": [
    {
      "name": "<item name>",
      "quantity": "<quantity with unit, e.g., '2 x 960g' or 'Quantity: 6'>",
      "price": <final price in rupees as a number>
    }
  ]
}

STRICT Rules:
1. For total_amount: Extract the EXACT FINAL/GRAND TOTAL amount shown (not item total, MRP, or subtotal)
   - Look for labels like "Total", "Grand Total", "To Pay", "Bill Total"
   - If on Items tab with category subtotals, sum all category totals to get grand total
2. DO NOT round numbers - extract EXACTLY as shown (e.g., if it says 94, return 94, NOT 100)
3. For items: Extract ALL ordered items with their EXACT quantities and EXACT FINAL prices (after discounts)
   - Items may be grouped under categories like "Snacks & Branded Foods", "Bakery, Cakes & Dairy" - extract ALL items from ALL categories
   - Item format: Brand name + Product name (e.g., "BRITANNIA Nutrichoice Digestive High-Fibre Biscuits")
   - Look for "Quantity: X" labels for quantity information
   - Price is the rupee amount shown next to each item (final price after discounts)
4. For delivery_charge: Extract exact amount. If it says "FREE" or "₹0", return 0
5. For handling_charge: Extract exact amount. If it says "FREE" or "₹0", return 0
6. If quantity has units (g, kg, ml, L, etc.), include them exactly as shown
   - Examples: "2 x 960g", "6 x 500ml", "Quantity: 1"
7. Clean up item names (remove checkmarks, extra symbols, but keep brand names)
8. Return ONLY valid JSON, no additional text
9. If you're unsure about any number, return an error instead of guessing
""" + identifier_instructions + """

EXAMPLES of what to extract:
- "BRITANNIA Nutrichoice Digestive High-Fibre Biscuits 960g ₹161.96 Quantity: 1"
  → {"name": "BRITANNIA Nutrichoice Digestive High-Fibre Biscuits", "quantity": "1 x 960g", "price": 161.96}
- "NANDINI GoodLife Toned Milk 500ml ₹186 Quantity: 6"
  → {"name": "NANDINI GoodLife Toned Milk", "quantity": "6 x 500ml", "price": 186}

If you cannot extract the information with certainty, return:
{"error": "Could not extract order details"}
"""

            # Generate content with AI - pass prompt + all images
            print("Extracting with Gemini AI...")
            content = [prompt] + images
            response = gemini_model.generate_content(content)

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

            # Ensure order_type exists with default "Blinkit"
            if "order_type" not in result:
                result["order_type"] = "Blinkit"

            print(f"✅ AI Extraction completed")
            print(f"   Order Type: {result['order_type']}")
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

def save_blinkit_items(telegram_name, items, total_charges, order_date, order_time, payment_from, order_type="Blinkit"):
    """
    Save each item as a separate row in Google Sheets with same Order ID

    Args:
        telegram_name: Name of telegram user
        items: List of item dictionaries with name, quantity, price
        total_charges: Sum of delivery + handling charges
        order_date: Date string
        order_time: Time string
        payment_from: Payment method selected by user
        order_type: Order type/supplier name (default: "Blinkit")
    """
    try:
        sheet = client.open_by_key(ALLOWANCE_SHEET_ID).worksheet(TAB_NAME_ALLOWANCE)

        headers = sheet.row_values(1)
        expected_headers = ["Order ID", "Date", "Time", "Telegram Name", "Item Name",
                           "Quantity", "Price", "Charges", "Order Type", "Payment from", "Category"]

        # Only update headers if they're missing or incomplete
        # Allow for extra columns that may be added manually
        if not headers or len(headers) < 11:
            sheet.update('A1:K1', [expected_headers])

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
                order_type,  # Detected order type from AI
                payment_from,  # Payment method
                ""  # Category (empty for bot entries, can be filled manually)
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
async def handle_start(update: Update, context):
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
✅ Ask you to select payment method
✅ Save each item as separate row in Google Sheets

That's it! Simple. 😊

✨ Works in groups too!
"""

    await update.message.reply_text(welcome_msg, parse_mode='Markdown')

async def handle_multi(update: Update, context):
    """Handle /multi command - start multi-screenshot collection mode"""
    user = update.message.from_user
    user_name = user.first_name or user.username or "there"

    # Initialize screenshots collection
    context.user_data['screenshots'] = []
    context.user_data['multi_mode'] = True
    context.user_data['user_display_name'] = user_name
    context.user_data['chat_type'] = update.effective_chat.type

    print(f"\n=== MULTI MODE ACTIVATED by {user_name} ===")

    await update.message.reply_text(
        f"📸 *Multi-Screenshot Mode Activated!*\n\n"
        f"Send multiple screenshots of the same order (for long orders with many items).\n\n"
        f"After each screenshot, you'll get buttons to:\n"
        f"• Add more screenshots\n"
        f"• Process all screenshots\n\n"
        f"💡 Tip: Take screenshots of different parts of the same order:\n"
        f"   - Top items\n"
        f"   - Middle items (scrolled)\n"
        f"   - Bottom with bill/total\n\n"
        f"Now send your first screenshot!",
        parse_mode='Markdown'
    )

async def handle_reset(update: Update, context):
    """Handle /reset command - cancel current operation and clear data"""
    # Clear all user data
    context.user_data.clear()

    await update.message.reply_text(
        "🔄 *Reset Complete*\n\n"
        "All data cleared. You can now:\n"
        "• Send a single screenshot for instant processing\n"
        "• Use /multi for multiple screenshots\n"
        "• Use /start to see the welcome message",
        parse_mode='Markdown'
    )

    return ConversationHandler.END

async def handle_photo(update: Update, context):
    """Handle photo messages - automatically process Blinkit screenshots"""
    user = update.message.from_user
    user_id = user.id
    user_first_name = user.first_name or ""
    user_username = user.username or ""
    user_display_name = user_first_name or user_username or f"User_{user_id}"

    chat_type = update.effective_chat.type  # 'private', 'group', or 'supergroup'

    # Check if we're in multi-screenshot mode
    if context.user_data.get('multi_mode'):
        # Multi-screenshot collection mode
        try:
            processing_msg = await update.message.reply_text("⏳ Downloading screenshot...")

            photo = update.message.photo[-1]
            print(f"[MULTI MODE] Photo {len(context.user_data.get('screenshots', [])) + 1} from {user_display_name}, file_size: {photo.file_size}")

            # Check file size before downloading (10MB limit)
            if photo.file_size > 10 * 1024 * 1024:
                await processing_msg.edit_text("❌ Image too large (max 10MB allowed).")
                return WAITING_FOR_MORE_PHOTOS

            file = await photo.get_file()
            image_bytes = await file.download_as_bytearray()

            # Add to screenshots collection
            context.user_data['screenshots'].append(bytes(image_bytes))
            num_screenshots = len(context.user_data['screenshots'])

            # Create inline keyboard
            keyboard = [
                [InlineKeyboardButton("➕ Add More Screenshots", callback_data="add_more_screenshots")],
                [InlineKeyboardButton("✅ Process All Screenshots", callback_data="process_all_screenshots")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await processing_msg.edit_text(
                f"📸 *Screenshot {num_screenshots} received!*\n\n"
                f"Total screenshots collected: {num_screenshots}\n\n"
                f"Choose an option:",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )

            return WAITING_FOR_MORE_PHOTOS

        except Exception as e:
            print(f"Error in multi-screenshot mode: {e}")
            import traceback
            traceback.print_exc()
            await update.message.reply_text("❌ Error processing screenshot. Use /reset to start over.")
            return WAITING_FOR_MORE_PHOTOS

    # Single screenshot mode (existing behavior - unchanged)
    try:
        processing_msg = await update.message.reply_text("⏳ Processing your order...")

        photo = update.message.photo[-1]
        print(f"Photo from {user_display_name}, file_size: {photo.file_size}")

        # Check file size before downloading (10MB limit)
        if photo.file_size > 10 * 1024 * 1024:
            await processing_msg.edit_text("❌ Image too large (max 10MB allowed).")
            return

        file = await photo.get_file()
        image_bytes = await file.download_as_bytearray()

        await processing_msg.edit_text("⏳ Extracting order details with AI...")

        # Use AI extraction
        result = extract_order_details_with_ai(bytes(image_bytes))

        # Check for quota error specifically
        if isinstance(result, dict) and result.get("error") == "quota_exceeded":
            # Store basic info for manual entry
            now = datetime.datetime.now(INDIA_TZ)
            context.user_data['manual_entry'] = {
                'user_display_name': user_display_name,
                'order_date': now.strftime("%Y-%m-%d"),
                'order_time': now.strftime("%H:%M:%S"),
                'now': now,
                'chat_type': chat_type
            }

            retry_after = result.get("retry_after", 30)
            await processing_msg.edit_text(
                f"⚠️ *API Quota Exceeded*\n\n"
                f"The Gemini AI quota has been reached. You can either:\n"
                f"• Wait {int(retry_after)} seconds and send the screenshot again\n"
                f"• Or enter the total amount manually now\n\n"
                f"Please enter the total amount (numbers only):\n"
                f"Example: 450 or 450.50",
                parse_mode='Markdown'
            )
            return WAITING_FOR_MANUAL_AMOUNT

        if not result or "total_amount" not in result:
            # Store basic info for manual entry
            now = datetime.datetime.now(INDIA_TZ)
            context.user_data['manual_entry'] = {
                'user_display_name': user_display_name,
                'order_date': now.strftime("%Y-%m-%d"),
                'order_time': now.strftime("%H:%M:%S"),
                'now': now,
                'chat_type': chat_type
            }

            await processing_msg.edit_text(
                "⚠️ Could not extract order details from the image.\n\n"
                "Please enter the total amount manually (numbers only):\n"
                "Example: 450 or 450.50"
            )
            return WAITING_FOR_MANUAL_AMOUNT

        total_amount = result["total_amount"]
        items = result.get("items", [])
        delivery_charge = result.get("delivery_charge", 0)
        handling_charge = result.get("handling_charge", 0)
        total_charges = delivery_charge + handling_charge
        order_type = result.get("order_type", "Blinkit")

        # Get current date and time
        now = datetime.datetime.now(INDIA_TZ)
        order_date = now.strftime("%Y-%m-%d")
        order_time = now.strftime("%H:%M:%S")

        # Store extracted data in context for later use
        context.user_data['pending_order'] = {
            'user_display_name': user_display_name,
            'items': items,
            'total_charges': total_charges,
            'total_amount': total_amount,
            'delivery_charge': delivery_charge,
            'handling_charge': handling_charge,
            'order_type': order_type,
            'order_date': order_date,
            'order_time': order_time,
            'now': now,
            'chat_type': chat_type
        }

        # Create inline keyboard with payment options
        keyboard = [
            [InlineKeyboardButton("Nishat Personal", callback_data="payment_Nishat Personal")],
            [InlineKeyboardButton("Nishat Company Card", callback_data="payment_Nishat Company Card")],
            [InlineKeyboardButton("Kim Company Card", callback_data="payment_Kim Company Card")],
            [InlineKeyboardButton("Kim Petty cash", callback_data="payment_Kim Petty cash")],
            [InlineKeyboardButton("Ajay personal", callback_data="payment_Ajay personal")],
            [InlineKeyboardButton("Ajay Company", callback_data="payment_Ajay Company")],
            [InlineKeyboardButton("Ayaaz personal", callback_data="payment_Ayaaz personal")],
            [InlineKeyboardButton("Ayaaz Company", callback_data="payment_Ayaaz Company")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Ask user to select payment method
        await processing_msg.edit_text(
            f"✅ Order extracted successfully!\n\n"
            f"💰 Total Amount: ₹{total_amount:.2f}\n"
            f"🛒 Items: {len(items)}\n\n"
            f"Please select the payment method:",
            reply_markup=reply_markup
        )

        return WAITING_FOR_PAYMENT_METHOD

    except BadRequest as e:
        # Catch the specific Telegram parsing error for cleaner output
        print(f"❌ Telegram Formatting Error: {e}")
        import traceback
        traceback.print_exc()
        try:
            await update.message.reply_text(
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
            await update.message.reply_text(
                "❌ Critical error processing image. Please try again or contact admin.\n\n"
                "You can send the screenshot again to retry."
            )
        except Exception:
             pass # Failsafe

async def handle_manual_amount(update: Update, context):
    """Handle manually entered amount when AI extraction fails"""
    user_input = update.message.text.strip()

    # Validate input is a number
    try:
        total_amount = float(user_input)
        if total_amount <= 0:
            await update.message.reply_text(
                "❌ Please enter a valid positive amount.\n"
                "Example: 450 or 450.50"
            )
            return WAITING_FOR_MANUAL_AMOUNT
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid amount. Please enter numbers only.\n"
            "Example: 450 or 450.50"
        )
        return WAITING_FOR_MANUAL_AMOUNT

    # Retrieve stored manual entry data
    manual_entry = context.user_data.get('manual_entry')
    if not manual_entry:
        await update.message.reply_text("❌ Session expired. Please send the screenshot again.")
        return ConversationHandler.END

    # Store the amount and prepare for payment method selection
    context.user_data['pending_order'] = {
        'user_display_name': manual_entry['user_display_name'],
        'items': [{'name': 'not processed', 'quantity': '1', 'price': total_amount}],
        'total_charges': 0,
        'total_amount': total_amount,
        'delivery_charge': 0,
        'handling_charge': 0,
        'order_type': 'Blinkit',  # Default for manual entries
        'order_date': manual_entry['order_date'],
        'order_time': manual_entry['order_time'],
        'now': manual_entry['now'],
        'chat_type': manual_entry['chat_type'],
        'manual_entry': True  # Flag to indicate this was manual
    }

    # Clear manual entry data
    context.user_data.pop('manual_entry', None)

    # Create inline keyboard with payment options
    keyboard = [
        [InlineKeyboardButton("Nishat Personal", callback_data="payment_Nishat Personal")],
        [InlineKeyboardButton("Nishat Company Card", callback_data="payment_Nishat Company Card")],
        [InlineKeyboardButton("Kim Company Card", callback_data="payment_Kim Company Card")],
        [InlineKeyboardButton("Kim Petty cash", callback_data="payment_Kim Petty cash")],
        [InlineKeyboardButton("Ajay personal", callback_data="payment_Ajay personal")],
        [InlineKeyboardButton("Ajay Company", callback_data="payment_Ajay Company")],
        [InlineKeyboardButton("Ayaaz personal", callback_data="payment_Ayaaz personal")],
        [InlineKeyboardButton("Ayaaz Company", callback_data="payment_Ayaaz Company")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Ask user to select payment method
    await update.message.reply_text(
        f"✅ Amount received: ₹{total_amount:.2f}\n\n"
        f"Please select the payment method:",
        reply_markup=reply_markup
    )

    return WAITING_FOR_PAYMENT_METHOD

async def handle_add_more_screenshots(update: Update, context):
    """Handle 'Add More Screenshots' button callback"""
    query = update.callback_query
    await query.answer()

    num_screenshots = len(context.user_data.get('screenshots', []))

    await query.edit_message_text(
        f"📸 *Ready for more screenshots!*\n\n"
        f"Currently have: {num_screenshots} screenshot(s)\n\n"
        f"Send the next screenshot of the same order.",
        parse_mode='Markdown'
    )

    return WAITING_FOR_MORE_PHOTOS

async def handle_process_all_screenshots(update: Update, context):
    """Handle 'Process All Screenshots' button - combine and extract data from all screenshots"""
    query = update.callback_query
    await query.answer()

    screenshots = context.user_data.get('screenshots', [])
    user_display_name = context.user_data.get('user_display_name', 'Unknown')
    chat_type = context.user_data.get('chat_type', 'private')

    if not screenshots:
        await query.edit_message_text("❌ No screenshots found. Please send screenshots first.")
        return ConversationHandler.END

    try:
        await query.edit_message_text(
            f"⏳ *Processing {len(screenshots)} screenshot(s)...*\n\n"
            f"Combining all items into one order. This may take a moment...",
            parse_mode='Markdown'
        )

        print(f"\n=== PROCESSING {len(screenshots)} SCREENSHOTS ===")

        # Run AI extraction in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, extract_order_details_with_ai, screenshots)

        print(f"AI extraction completed! Result: {result is not None}")

        # Clear multi mode and screenshots
        context.user_data.pop('multi_mode', None)
        context.user_data.pop('screenshots', None)

        # Check for quota error
        if isinstance(result, dict) and result.get("error") == "quota_exceeded":
            now = datetime.datetime.now(INDIA_TZ)
            context.user_data['manual_entry'] = {
                'user_display_name': user_display_name,
                'order_date': now.strftime("%Y-%m-%d"),
                'order_time': now.strftime("%H:%M:%S"),
                'now': now,
                'chat_type': chat_type
            }

            retry_after = result.get("retry_after", 30)
            await query.edit_message_text(
                f"⚠️ *API Quota Exceeded*\n\n"
                f"The Gemini AI quota has been reached. You can either:\n"
                f"• Wait {int(retry_after)} seconds and try again\n"
                f"• Or enter the total amount manually now\n\n"
                f"Please enter the total amount (numbers only):\n"
                f"Example: 450 or 450.50",
                parse_mode='Markdown'
            )
            return WAITING_FOR_MANUAL_AMOUNT

        # Check if extraction failed
        if not result or "total_amount" not in result:
            now = datetime.datetime.now(INDIA_TZ)
            context.user_data['manual_entry'] = {
                'user_display_name': user_display_name,
                'order_date': now.strftime("%Y-%m-%d"),
                'order_time': now.strftime("%H:%M:%S"),
                'now': now,
                'chat_type': chat_type
            }

            await query.edit_message_text(
                "⚠️ Could not extract order details from the screenshots.\n\n"
                "Please enter the total amount manually (numbers only):\n"
                "Example: 450 or 450.50"
            )
            return WAITING_FOR_MANUAL_AMOUNT

        # Extract order details
        total_amount = result["total_amount"]
        items = result.get("items", [])
        delivery_charge = result.get("delivery_charge", 0)
        handling_charge = result.get("handling_charge", 0)
        total_charges = delivery_charge + handling_charge
        order_type = result.get("order_type", "Blinkit")

        now = datetime.datetime.now(INDIA_TZ)
        order_date = now.strftime("%Y-%m-%d")
        order_time = now.strftime("%H:%M:%S")

        # Store extracted data
        context.user_data['pending_order'] = {
            'user_display_name': user_display_name,
            'items': items,
            'total_charges': total_charges,
            'total_amount': total_amount,
            'delivery_charge': delivery_charge,
            'handling_charge': handling_charge,
            'order_type': order_type,
            'order_date': order_date,
            'order_time': order_time,
            'now': now,
            'chat_type': chat_type
        }

        # Create payment options keyboard
        keyboard = [
            [InlineKeyboardButton("Nishat Personal", callback_data="payment_Nishat Personal")],
            [InlineKeyboardButton("Nishat Company Card", callback_data="payment_Nishat Company Card")],
            [InlineKeyboardButton("Kim Company Card", callback_data="payment_Kim Company Card")],
            [InlineKeyboardButton("Kim Petty cash", callback_data="payment_Kim Petty cash")],
            [InlineKeyboardButton("Ajay personal", callback_data="payment_Ajay personal")],
            [InlineKeyboardButton("Ajay Company", callback_data="payment_Ajay Company")],
            [InlineKeyboardButton("Ayaaz personal", callback_data="payment_Ayaaz personal")],
            [InlineKeyboardButton("Ayaaz Company", callback_data="payment_Ayaaz Company")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"✅ *Order extracted successfully!*\n\n"
            f"📦 Combined from {len(screenshots)} screenshot(s)\n"
            f"💰 Total Amount: ₹{total_amount:.2f}\n"
            f"🛒 Items: {len(items)}\n"
            f"🚚 Order Type: {order_type}\n\n"
            f"Please select the payment method:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

        return WAITING_FOR_PAYMENT_METHOD

    except Exception as e:
        print(f"Error processing screenshots: {e}")
        import traceback
        traceback.print_exc()
        context.user_data.pop('multi_mode', None)
        context.user_data.pop('screenshots', None)
        await query.edit_message_text(
            "❌ Error processing screenshots. Please use /reset and try again."
        )
        return ConversationHandler.END

async def handle_payment_selection(update: Update, context):
    """Handle payment method selection"""
    query = update.callback_query
    await query.answer()

    # Extract payment method from callback data
    payment_method = query.data.replace("payment_", "")

    # Retrieve stored order data
    pending_order = context.user_data.get('pending_order')

    if not pending_order:
        await query.edit_message_text("❌ Session expired. Please send the screenshot again.")
        return ConversationHandler.END

    # Extract all the stored data
    user_display_name = pending_order['user_display_name']
    items = pending_order['items']
    total_charges = pending_order['total_charges']
    total_amount = pending_order['total_amount']
    delivery_charge = pending_order['delivery_charge']
    handling_charge = pending_order['handling_charge']
    order_type = pending_order.get('order_type', 'Blinkit')
    order_date = pending_order['order_date']
    order_time = pending_order['order_time']
    now = pending_order['now']
    chat_type = pending_order['chat_type']

    # Update message to show saving status
    await query.edit_message_text("⏳ Saving to sheet...")

    # Save to sheet with payment method and order type
    success, order_id = save_blinkit_items(
        user_display_name,
        items,
        total_charges,
        order_date,
        order_time,
        payment_method,
        order_type
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
            f"💳 *Payment Method: {payment_method}*",
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

        await query.edit_message_text("\n".join(confirmation), parse_mode='Markdown')
    else:
        await query.edit_message_text(
            "❌ Error saving to sheet. Please try again or contact admin.\n\n"
            "You can send the screenshot again to retry."
        )

    # Clear pending order data
    context.user_data.pop('pending_order', None)

    return ConversationHandler.END

async def handle_text(update: Update, context):
    """Handle text messages"""
    await update.message.reply_text(
        "📸 Please send a screenshot of your Blinkit/Instamart order.\n\n"
        "I can only process images/screenshots, not text messages."
    )

async def post_init(application):
    """Set up bot commands menu"""
    from telegram import BotCommand

    commands = [
        BotCommand("start", "Show welcome message and instructions"),
        BotCommand("multi", "Start multi-screenshot mode for long orders"),
        BotCommand("reset", "Cancel current operation and clear data")
    ]

    await application.bot.set_my_commands(commands)
    print("✅ Bot commands menu configured")

def setup_handlers(app):
    """Setup message handlers"""
    # Command handlers
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("multi", handle_multi))
    app.add_handler(CommandHandler("reset", handle_reset))

    # Conversation handler for photo processing with payment method selection
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.PHOTO, handle_photo)
        ],
        states={
            WAITING_FOR_MORE_PHOTOS: [
                MessageHandler(filters.PHOTO, handle_photo),
                CallbackQueryHandler(handle_add_more_screenshots, pattern="^add_more_screenshots$"),
                CallbackQueryHandler(handle_process_all_screenshots, pattern="^process_all_screenshots$")
            ],
            WAITING_FOR_MANUAL_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_amount)
            ],
            WAITING_FOR_PAYMENT_METHOD: [
                CallbackQueryHandler(handle_payment_selection, pattern="^payment_")
            ],
        },
        fallbacks=[CommandHandler("reset", handle_reset)],
        allow_reentry=True,
        per_message=True
    )

    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

# === Main Entry Point ===
if __name__ == "__main__":
    print("Blinkit Bot starting...")
    print("Features:")
    print("- Each item saved as separate row")
    print("- Extracts delivery & handling charges")
    print("- Only captures Telegram name (no username/ID)")

    # Build the application
    application = Application.builder().token(BLINKIT_BOT_TOKEN).post_init(post_init).build()

    setup_handlers(application)
    print("Bot handlers configured")
    print("Ready to process Blinkit/Instamart order screenshots automatically.")
    print("Press Ctrl+C to stop the bot.")

    # Start polling for updates
    application.run_polling()
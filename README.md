# Blinkit Order Bot

A Telegram bot that automatically processes Blinkit/Instamart order screenshots using AI and saves the data to Google Sheets.

## Features

- Automatic screenshot processing with Google Gemini AI
- Extracts total amount, items, quantities, and prices
- Extracts delivery and handling charges
- Saves each item as a separate row in Google Sheets
- Works in private chats and group chats
- Generates unique Order IDs for tracking

## Prerequisites

1. Telegram Bot Token (from @BotFather)
2. Google Cloud Project with:
   - Vision API enabled
   - Gemini API key
   - Service Account with Google Sheets API access
3. Google Sheet for storing orders

## Setup Instructions

### 1. Clone the Repository

```bash
git clone <your-repo-url>
cd <your-repo-name>
```

### 2. Install Dependencies Locally (Optional)

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Edit `.env` and add your credentials:
```
BLINKIT_BOT_TOKEN=your_telegram_bot_token
GEMINI_API_KEY=your_gemini_api_key
```

### 4. Add Google Service Account JSON

Create a Google Cloud Service Account and download the JSON key file.
Rename it to `service_account.json` and place it in the project root.

**IMPORTANT:** Never commit this file to Git (it's in .gitignore)

### 5. Configure Google Sheets

Update the following in `blinkit-bot.py`:
- `ALLOWANCE_SHEET_ID`: Your Google Sheet ID
- `TAB_NAME_ALLOWANCE`: Your sheet tab name

## Deployment to Render

### Option 1: Using render.yaml (Recommended)

1. Push your code to GitHub
2. Go to [Render Dashboard](https://dashboard.render.com/)
3. Click "New +" and select "Blueprint"
4. Connect your GitHub repository
5. Render will automatically detect `render.yaml`
6. Add environment variables in Render dashboard:
   - `BLINKIT_BOT_TOKEN`
   - `GEMINI_API_KEY`
7. Add `service_account.json` as a secret file in Render

### Option 2: Manual Setup

1. Push your code to GitHub
2. Go to [Render Dashboard](https://dashboard.render.com/)
3. Click "New +" and select "Web Service"
4. Connect your GitHub repository
5. Configure:
   - **Name:** blinkit-bot
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python blinkit-bot.py`
6. Add environment variables
7. Add `service_account.json` as a secret file

### Adding service_account.json to Render

1. In your Render service, go to "Environment"
2. Scroll to "Secret Files"
3. Click "Add Secret File"
4. Filename: `service_account.json`
5. Contents: Paste your entire JSON file
6. Save

## Local Testing

Run the bot locally:
```bash
python blinkit-bot.py
```

## Usage

1. Start a chat with your bot on Telegram
2. Send `/start` to see instructions
3. Send a screenshot of your Blinkit/Instamart order
4. The bot will automatically extract and save the details

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `BLINKIT_BOT_TOKEN` | Telegram Bot API token | Yes |
| `GEMINI_API_KEY` | Google Gemini API key | Yes |

## File Structure

```
.
├── blinkit-bot.py          # Main bot code
├── requirements.txt        # Python dependencies
├── render.yaml            # Render deployment config
├── .env.example           # Environment variables template
├── .gitignore            # Git ignore rules
├── service_account.json  # Google credentials (DO NOT COMMIT)
└── README.md             # This file
```

## Troubleshooting

### Bot not responding
- Check if the bot is running in Render logs
- Verify environment variables are set correctly
- Check Telegram bot token is valid

### API Quota Exceeded
- Gemini API has rate limits on free tier
- Wait for the quota to reset or upgrade your API plan

### Google Sheets errors
- Verify service account has edit access to the sheet
- Check sheet ID and tab name are correct
- Ensure Google Sheets API is enabled

## License

MIT

## Support

For issues and questions, please open an issue on GitHub.

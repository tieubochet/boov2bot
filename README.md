# TeeBoo Assistant Bot

**TeeBoo Assistant** is a multifunctional Telegram bot built with Python and Flask, designed to run efficiently on serverless platforms like Vercel. The bot serves as a personal and community assistant, offering powerful tools for task management and cryptocurrency market tracking.

## ✨ Key Features

### 🗓️ Task Management & Reminders
- **Add, Edit, Delete, and List Tasks:** Easily manage your schedule with simple commands.
- **Automatic Reminders:** The bot automatically sends a reminder 30 minutes before a task is due.
- **Pin & Notify:** Reminder messages are automatically **pinned** and **sent with a notification** to all group members, ensuring no one misses important events (requires Admin privileges).
- **Smart Workflow:** After every action that modifies the task list (add, edit, delete), the bot automatically displays the updated list, keeping the chat clean and informative.

### 📈 Powerful Crypto Tools
- **Instant Price Check (`/gia`):** Quickly check the price of popular cryptocurrencies.
- **Crypto Calculator (`/calc`):** Calculate the USD value of any amount of a specific token.
- **Token Lookup (`Send Contract Address`):** Automatically scans multiple blockchains (BSC, ETH, Tron, etc.) to find detailed information about a token when you send its contract address.
- **Portfolio Management (`Send List`):** Calculates the total value of a portfolio with multiple different tokens. Includes a convenient "Refresh" button for instant price updates.
- **AI Assistant (`/gt`):** Explains crypto terms, concepts, or answers any related questions using the power of the Google Gemini API.
- **Specialized Translator (`/tr`):** Acts as a financial interpreter, translating English text into Vietnamese using accurate, context-aware terminology.
- **Kaito Rank Check (`/ktrank`):** Fetches a user's ranking and mindshare information from the Kaito API.

### 🚀 Architecture
- **Serverless Optimized:** Designed to run smoothly on platforms like Vercel.
- **Persistent Storage:** Utilizes Vercel KV (Redis) to securely store all task data, ensuring no data loss.
- **Automated Operations:** Leverages an external service (like UptimeRobot) to trigger periodic tasks such as reminders.

## 🛠️ Setup & Deployment Guide

To deploy this bot, you'll need to follow these steps.

### 1. Prepare API Keys and Tokens
You will need to obtain credentials from the following services:

- **Telegram Bot Token:**
  1. Talk to [@BotFather](https://t.me/BotFather) on Telegram.
  2. Create a new bot using the `/newbot` command.
  3. Copy the **HTTP API token** provided by BotFather.

- **Vercel KV (Redis) URL:**
  1. Create a new project on [Vercel](https://vercel.com/).
  2. In your project dashboard, go to the "Storage" tab.
  3. Click "Connect Store", select "KV (Serverless Redis)", and follow the instructions.
  4. Once connected, Vercel will automatically create the necessary environment variables. You just need to copy the value of the variable named `KV_URL` or `TEEBOOV2_REDIS_URL` (depending on your project name).

- **Google Gemini API Key:**
  1. Go to [Google AI Studio](https://aistudio.google.com/app/apikey).
  2. Log in with your Google account.
  3. Click **"Create API key"** to generate a new key and copy it.

- **CRON Secret:**
  1. This is a secret string that **you create yourself** to protect the reminder endpoint. Example: `MySuperSecretKeyForCron123`.

### 2. Prepare the Source Code
1. Clone this repository to your local machine.
2. Create a file named `requirements.txt` with the following content:
   ```
   Flask
   requests
   pytz
   redis
   google-generativeai
   ```
3. Ensure your main Python file is named `app.py` (or configure a different name on Vercel).

### 3. Deploy to Vercel
1. Connect your GitHub repository to Vercel.
2. In your Vercel project settings (Project Settings -> Environment Variables), create the following variables:
   - `TELEGRAM_TOKEN`: Your bot token.
   - `teeboov2_REDIS_URL`: Your Vercel KV URL.
   - `CRON_SECRET`: The secret string you created.
   - `GOOGLE_API_KEY`: The API Key from Google AI Studio.
3. Deploy the project. Vercel will provide you with a public URL (e.g., `https://your-app-name.vercel.app`).

### 4. Set the Telegram Webhook
Run the following command in your terminal (replace `YOUR_BOT_TOKEN` and `YOUR_VERCEL_URL`):
```bash
curl "https://api.telegram.org/botYOUR_BOT_TOKEN/setWebhook?url=YOUR_VERCEL_URL"
```

### 5. Set up a Cron Job Service (for Reminders)
We'll use [Cron-job](https://cron-job.org/en/) as it's free and easy to set up.
1. Sign up for a free account on Cron-job.
2. On the Dashboard, click **"+ Add New Monitor"**.
3. **Monitor Type:** Select `HTTP(s)`.
4. **Friendly Name:** Choose any name (e.g., "My Telegram Bot Reminder").
5. **URL (or IP):** Paste your endpoint URL: `https://your-app-name.vercel.app/check_reminders`.
6. **Monitoring Interval:** Select "5 minutes".
7. Open **"Advanced Settings"**:
   - **HTTP Method:** Select `POST`.
   - **Content-Type:** Select `application/json`.
   - **Post-data(JSON):** Paste the following (replace with your actual `CRON_SECRET`):
     ```json
     { "secret": "MySuperSecretKeyForCron123" }
     ```
8. Click **"Create Monitor"**.

### 6. Grant Admin Privileges to the Bot (Required)
1. Add the bot to your Telegram group.
2. Promote the bot to an **Administrator**.
3. Enable the **"Pin Messages"** permission for the bot.

**Done!** Your bot is now fully operational.

## 📜 Command List

*   `/start` - Displays a welcome message and the command list.

**Task Management:**
*   `/add DD/MM HH:mm - Task Name` - Adds a new task.
*   `/edit <number> DD/MM HH:mm - New Name` - Edits an existing task.
*   `/del <number>` - Deletes a task.
*   `/list` - Shows the list of upcoming tasks.

**Crypto Tools:**
*   `/gia <symbol>` - Gets the price of a token (e.g., `/gia btc`).
*   `/calc <symbol> <amount>` - Calculates the USD value of a token amount.
*   `/gt <question>` - Explains a crypto term or concept.
*   `/tr <english text>` - Translates text to Vietnamese with financial context.
*   `/ktrank <username>` - Checks a user's Kaito ranking.

**Automatic:**
*   **Send a contract address:** The bot will automatically look up token information.
*   **Send a portfolio list:** The bot will automatically calculate the total value.
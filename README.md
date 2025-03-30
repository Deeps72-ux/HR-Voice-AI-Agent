# HR Voice AI Agent

[![Python Version](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![Flask Version](https://img.shields.io/badge/flask-3.x-green.svg)](https://flask.palletsprojects.com/)
[![Twilio Version](https://img.shields.io/badge/twilio-9.x-red.svg)](https://www.twilio.com/docs/libraries/python)
[![Google Cloud AI Platform](https://img.shields.io/badge/Vertex%20AI-Gemini-4285F4.svg)](https://cloud.google.com/vertex-ai)
[![ElevenLabs](https://img.shields.io/badge/ElevenLabs-TTS-orange.svg)](https://elevenlabs.io/)

An intelligent voice agent built with Python, Flask, Twilio, Google Gemini, and ElevenLabs to automate the initial stages of HR candidate outreach and interview scheduling.

Inspired by the philosophy of empowering HR teams by balancing AI automation with human connection, this agent handles repetitive scheduling tasks, freeing up HR professionals for more meaningful candidate interactions.

## Features

* **Automated Candidate Sourcing:** Fetches candidate details (Name, Phone, Email, Job Title, Interviewer, Available Slots) from a designated Google Sheet based on a "To Call" status.
* **Outbound Calling:** Initiates voice calls to candidates using Twilio Programmable Voice.
* **AI-Powered Conversation:** Utilizes Google Gemini (via Vertex AI) for a state-driven conversation:
    * Verifies candidate identity.
    * Checks if it's a good time to talk.
    * Proposes available interview slots parsed from the Google Sheet.
    * Handles candidate confirmation or rejection of proposed slots.
    * Attempts to answer basic candidate questions about the job/company using provided text files (`job_description.txt`, `companyinfo.txt`).
    * Confirms details before ending the call appropriately.
* **Natural Voice:** Uses ElevenLabs API for high-quality, natural-sounding Text-to-Speech (TTS). Includes fallback to standard Twilio Say.
* **Google Calendar Integration:** Automatically schedules the confirmed interview on Google Calendar, inviting both the candidate and the interviewer (using Google User OAuth Credentials). Includes a Google Meet link.
* **Google Sheets Update:** Updates the candidate's status in the Google Sheet (e.g., "Scheduled", "No Answer / Failed", "Booking Failed") along with notes and the scheduled time.
* **Webhook Handling:** Uses Flask to handle incoming webhooks from Twilio for call progress, user speech input, and final call status.
* **State Management:** Employs a robust state machine managed via Flask session to guide the conversation flow.
* **Background Processing:** Calendar booking and sheet updates are handled in a background thread to prevent blocking call responses.

## Tech Stack

* **Backend:** Python 3.9+
* **Framework:** Flask
* **Voice & Telephony:** Twilio Programmable Voice API
* **Conversational AI:** Google Gemini Pro (via Vertex AI SDK)
* **Text-to-Speech (TTS):** ElevenLabs API
* **Data/Scheduling:**
    * Google Sheets API (for candidate data & status)
    * Google Calendar API (for event scheduling)
* **Authentication:**
    * Google Cloud Service Account (for Sheets & Vertex AI)
    * Google Cloud User OAuth 2.0 (for Calendar - requires user consent flow initially)
* **Other Libraries:** `requests`, `python-dotenv`, `pytz`, `python-dateutil`, `google-auth`, etc. (see `requirements.txt`)

## Architecture Flow (Simplified)

1.  **Trigger:** Application starts (`app.py`).
2.  **Get Candidate:** Reads Google Sheet via Sheets API to find a candidate with status "To Call".
3.  **Initiate Call:** Uses Twilio API to place an outbound call to the candidate's phone number. Twilio is pointed to the app's `/voice` webhook.
4.  **Initial Contact (`/voice`):**
    * Flask receives the call webhook.
    * Initializes conversation state (`START`).
    * Calls Gemini (`process_with_gemini`) for the introductory message.
    * Generates audio using ElevenLabs (`generate_elevenlabs_audio`).
    * Responds with TwiML `<Play>` (audio) and `<Gather>` (to collect candidate's speech), pointing Gather to `/process-voice`.
5.  **Conversation Turn (`/process-voice`):**
    * Flask receives speech input from Twilio `<Gather>`.
    * Retrieves current state from session.
    * Sends user input, state, history, and context to Gemini (`process_with_gemini`).
    * Gemini returns AI response text, next state, and context (like `confirmed_slot_iso`).
    * Updates session state.
    * Generates audio for AI response via ElevenLabs.
    * **If Slot Confirmed:** Dispatches background thread (`background_calendar_booking`) to create Google Calendar event and update Google Sheet status to "Scheduled".
    * Responds with TwiML `<Play>` and `<Gather>` (if more input needed) OR `<Play>` and `<Hangup>` (if conversation ends).
6.  **Call End (`/call-status` / `/reprompt`):**
    * Flask receives final status webhook from Twilio.
    * Updates Google Sheet for non-completed statuses (e.g., "No Answer / Failed", "Timeout").
    * Cleans up session data.

## Setup and Installation

1.  **Clone the Repository:**
    ```bash
    git clone <your-repository-url>
    cd HR-Voice-AI-Agent
    ```

2.  **Create and Activate Virtual Environment:**
    ```bash
    # Create venv (use python3 if needed)
    python -m venv venv
    # Activate venv
    # Windows:
    .\venv\Scripts\activate
    # macOS/Linux:
    source venv/bin/activate
    ```

3.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Obtain Credentials and API Keys:**

    * **Google Cloud Service Account:**
        * Create a Service Account in your Google Cloud project.
        * Enable the **Google Sheets API** and **Vertex AI API**.
        * Assign necessary roles (e.g., "Vertex AI User", "Editor" or finer-grained roles for Sheets).
        * Download the JSON key file for this service account.
    * **Google Cloud User OAuth Credentials:**
        * Enable the **Google Calendar API**.
        * Create OAuth 2.0 Client ID credentials (Type: Desktop App or Web App depending on your setup for `generate_token.py`). Download the `credentials.json` file.
        * Run a script (like a separate `generate_token.py`, not included here but standard for Google OAuth) using `credentials.json` with the scope `https://www.googleapis.com/auth/calendar.events`. This initial run requires user consent via a browser.
        * This will generate the `token.pickle` file containing the user's refresh token, which the application will use to access the calendar. Place `token.pickle` in the project's root directory.
    * **Twilio:**
        * Sign up for a Twilio account.
        * Get your Account SID and Auth Token from the Twilio Console dashboard.
        * Purchase or verify a Twilio Phone Number capable of making voice calls.
    * **ElevenLabs:**
        * Sign up for an ElevenLabs account.
        * Get your API Key from your profile settings. You might also want to note a specific Voice ID if not using the default.

5.  **Configure Environment Variables:**
    * Create a file named `.env` in the project's root directory.
    * Copy the contents of `.env.example` (see below) into `.env` and fill in your actual credentials and paths.

    **`.env.example`:**
    ```text
    # --- Twilio Configuration ---
    TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx # Your unique Twilio Account SID
    TWILIO_AUTH_TOKEN=your_twilio_auth_token          # Your Twilio account's secret authentication token
    TWILIO_PHONE_NUMBER=+15551234567                  # The Twilio phone number the calls will come from

    # --- Google Cloud Configuration ---
    # Full file path to your Service Account JSON key file (for Sheets/Vertex AI)
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/your/service-account-key.json
    # The ID of the Google Sheet holding candidate data (from sheet URL)
    SHEET_ID=your_google_sheet_id
    # Google Calendar ID to schedule on ('primary' or specific calendar email)
    CALENDAR_ID=primary

    # --- ElevenLabs Configuration ---
    ELEVENLABS_API_KEY=your_elevenlabs_api_key
    # Optional: Specify a voice ID, otherwise defaults might be used in code
    # ELEVENLABS_VOICE_ID=your_elevenlabs_voice_id

    # --- Application Deployment URL ---
    # Publicly accessible URL where this app runs (e.g., ngrok, Render) - NO trailing slash!
    PUBLIC_BASE_URL=[https://your-publicly-accessible-url.com](https://your-publicly-accessible-url.com)

    # --- Flask Session Secret Key ---
    # A strong, random secret key for Flask session management
    FLASK_SECRET_KEY=your_strong_random_secret_key
    ```

6.  **Create Input Files:**
    * Create `job_description.txt`: Paste the job description text here. The AI will use this to answer basic questions.
    * Create `companyinfo.txt`: Add general company information here for the AI to potentially use.

7.  **Set up Google Sheet:**
    * Create a Google Sheet with the `SHEET_ID` you specified in `.env`.
    * Ensure it has a sheet named `candidates`.
    * Required columns (based on `get_candidate_to_call`):
        * Col A: (Any ID)
        * Col B: Candidate Name (e.g., `name_idx=1`)
        * Col C: Phone Number (E.164 format preferable) (e.g., `phone_idx=2`)
        * Col D: Email Address (e.g., `email_idx=3`)
        * Col F: Job Title (e.g., `title_idx=5`)
        * Col I: **Status** (e.g., `status_col=8`). The agent looks for `To Call`. It will update this column.
        * Col J: Notes (Agent updates this)
        * Col L: Interviewer Email (e.g., `int_idx=11`)
        * Col M: Available Slots (ISO 8601 format, comma or semicolon separated, e.g., `2025-04-15T10:00:00+05:30;2025-04-15T14:30:00+05:30`) (e.g., `slots_idx=12`)
        * Col N: Scheduled Time (Agent updates this upon successful booking)
    * Make sure the **Service Account** email address (from the JSON key file) has **Editor** access to this Google Sheet.

## Usage

1.  **Ensure Prerequisites:** Make sure all setup steps (venv, pip install, credentials, `.env` file, input files, Sheet setup) are complete.
2.  **Expose Your App:** Your Flask application needs to be reachable by Twilio. During development, use a tool like `ngrok` to expose your local server:
    ```bash
    # Installs ngrok if you don't have it integrated
    # pip install pyngrok
    ngrok http 5000
    ```
    Copy the `https://` forwarding URL provided by ngrok and set it as your `PUBLIC_BASE_URL` in the `.env` file (remember: no trailing slash `/`).
3.  **Run the Flask App:**
    ```bash
    # Ensure your virtual environment is active
    python app.py
    ```
4.  **Trigger:** The application is currently configured to automatically attempt `run_one_time_call` on startup (when not using the Flask reloader's initial process). This will query the sheet for a candidate marked "To Call" and initiate the first call.
5.  **Monitor:** Observe the Flask console output for logs, including call progress, Gemini interactions, booking triggers, and potential errors. Check `hr_agent_conversations.log` for turn-by-turn details.

## Important Notes

* **Public URL:** The `PUBLIC_BASE_URL` is critical for Twilio webhooks. Ensure it's correctly set and points to your running application.
* **Credentials Security:** Keep your `.env` file, Service Account JSON key, and `token.pickle` secure. Do **not** commit them to public Git repositories. Add them to your `.gitignore` file.
* **Error Handling:** While logging is implemented, review logs carefully, especially for background task failures related to Google Calendar or Sheets API calls (check permissions, quotas, data validity).
* **Costs:** Be mindful of costs associated with Twilio calls, Vertex AI usage, ElevenLabs API calls, and other Google Cloud services.

## License

The Unlicense

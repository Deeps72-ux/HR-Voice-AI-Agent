# app.py (Final - OAuth Calendar, SA Sheets/Vertex, ElevenLabs, Local JD, Corrected Token Save)

import os
import logging
from flask import Flask
from dotenv import load_dotenv
import sys
import time
import atexit # Ensure this import is present
import json
import pickle # For loading user token

# --- Imports for Google Auth ---
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
# ------------------------------
try:
    # Ensure routes are importable from 'app' subdirectory
    from app.routes import configure_routes, get_candidate_to_call
except ImportError as e:
    logging.error(f"Could not import from 'app.routes'. Ensure routes.py exists in 'app/' folder: {e}")
    sys.exit(1)
except ModuleNotFoundError as e:
     logging.error(f"Could not find 'app' directory/module. Is routes.py inside 'app/'?: {e}")
     sys.exit(1)

try:
    # Google API client builder
    from googleapiclient.discovery import build
    # Twilio client
    from twilio.rest import Client
    # Vertex AI libraries
    from google.cloud import aiplatform
    import vertexai
    from vertexai.generative_models import GenerativeModel
    # HTTP requests library
    import requests
except ImportError as e:
     logging.error(f"Missing essential libraries: {e}. Install them (google-auth-oauthlib google-auth-httplib2 google-api-python-client Flask python-dotenv pytz twilio google-cloud-aiplatform vertexai requests python-dateutil).")
     sys.exit(1)

# --- Load Environment Variables & Basic Config ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.info("--- Starting app.py ---")

# --- Configuration Values ---
CREDENTIALS_PATH_SA = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") # Path to Service Account JSON
SHEET_ID = os.getenv("GID")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
CALENDAR_ID = os.getenv("CID", "primary") # User's primary calendar or specific ID
TOKEN_PICKLE_FILE = 'token.pickle' # User OAuth token
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "90ipbRoKi4CpHXvKVtl0") # Default Ayushi voice

# --- Validate Essential Config ---
essential_configs = {
    "GOOGLE_APPLICATION_CREDENTIALS (Service Account)": CREDENTIALS_PATH_SA,
    "GID (Sheet ID)": SHEET_ID,
    "TWILIO_ACCOUNT_SID": TWILIO_ACCOUNT_SID, "TWILIO_AUTH_TOKEN": TWILIO_AUTH_TOKEN,
    "TWILIO_PHONE_NUMBER": TWILIO_PHONE_NUMBER, "PUBLIC_BASE_URL": PUBLIC_BASE_URL,
    "CID (Calendar ID)": CALENDAR_ID,
    "ELEVENLABS_API_KEY": ELEVENLABS_API_KEY
}
missing_configs = [name for name, value in essential_configs.items() if not value]
if missing_configs:
    logging.error(f"❌ Missing essential environment variables: {', '.join(missing_configs)}. Please set them in your .env file.")
    sys.exit(1)
else:
    logging.info("✅ Essential environment variables loaded.")

if not os.path.exists(TOKEN_PICKLE_FILE):
     logging.error(f"❌ User token file '{TOKEN_PICKLE_FILE}' not found. Calendar features WILL FAIL. Run generate_token.py.")
     # Consider exiting if calendar is absolutely essential from the start
     # sys.exit(1)

# --- Load Local Text Files ---
def load_text_file(filename, description):
    """Loads text from a file, handles errors."""
    text_content = ""
    try:
        # Assume file is in the same directory as app.py
        filepath = os.path.join(os.path.dirname(__file__), filename)
        if not os.path.exists(filepath):
             raise FileNotFoundError(f"{description} file '{filename}' not found in script directory.")
        with open(filepath, 'r', encoding='utf-8') as f:
            text_content = f.read()
        logging.info(f"✅ Successfully loaded {description} from {filename}")
        if not text_content.strip():
            logging.warning(f"⚠️ File '{filename}' is empty.")
    except FileNotFoundError as e:
         logging.warning(f"⚠️ {e}") # Allow running without optional files
    except Exception as e:
         logging.error(f"❌ Error reading '{filename}': {e}")
    return text_content

COMPANY_INFO_FILENAME = "companyinfo.txt"
JOB_DESCRIPTION_FILENAME = "job_description.txt" # Read JD from this file
COMPANY_INFO_TEXT = load_text_file(COMPANY_INFO_FILENAME, "Company Info")
JOB_DESCRIPTION_TEXT = load_text_file(JOB_DESCRIPTION_FILENAME, "Job Description")
#----------------------------------

# --- Initialize Clients and Model ---
sheets_service = None; twilio_client = None; calendar_service = None; gemini_model = None;
service_account_creds = None; user_creds = None
_SHEETS_INIT_SUCCESS = False; _TWILIO_INIT_SUCCESS = False; _CALENDAR_INIT_SUCCESS = False; _VERTEX_INIT_SUCCESS = False
PROJECT_ID = None; LOCATION = None

# --- Load Service Account Credentials (for Sheets, Vertex AI) ---
try:
    if not CREDENTIALS_PATH_SA or not os.path.exists(CREDENTIALS_PATH_SA): raise FileNotFoundError(f"SA file not found: {CREDENTIALS_PATH_SA}")
    SCOPES_SA = ["https://www.googleapis.com/auth/cloud-platform", "https://www.googleapis.com/auth/spreadsheets"]
    service_account_creds = ServiceAccountCredentials.from_service_account_file(CREDENTIALS_PATH_SA, scopes=SCOPES_SA)
    logging.info(f"✅ Service account credentials loaded.")
    # Extract Project ID/Location
    try:
        with open(CREDENTIALS_PATH_SA, 'r') as f: service_account_info = json.load(f)
        PROJECT_ID = service_account_info.get("project_id")
        LOCATION = service_account_info.get("location", os.getenv("LOCATION", "us-central1"))
        if not PROJECT_ID: logging.error("❌ 'project_id' missing in SA credentials.")
        else: logging.info(f"   Using Project ID: {PROJECT_ID}, Location: {LOCATION}")
    except Exception as e: logging.error(f"   Error reading project/location from SA file: {e}")
    # Init Sheets
    if SHEET_ID and service_account_creds:
        try: sheets_service = build("sheets", "v4", credentials=service_account_creds); _SHEETS_INIT_SUCCESS = True; logging.info("✅ Sheets Service initialized (SA).")
        except Exception as e: logging.error(f"❌ Sheets Service init failed: {e}")
    else: logging.warning("⚠️ Sheets Service setup skipped (GID or SA creds invalid/missing).")
    # Init Vertex AI
    if PROJECT_ID and LOCATION and service_account_creds:
        try:
            aiplatform.init(project=PROJECT_ID, location=LOCATION, credentials=service_account_creds)
            gemini_model_name = "gemini-1.5-flash-001"; gemini_model = GenerativeModel(gemini_model_name);
            _VERTEX_INIT_SUCCESS = True; logging.info(f"✅ Vertex AI SDK & Gemini Model loaded (SA).")
        except Exception as e: logging.error(f"❌ Vertex AI/Gemini initialization failed: {e}")
    else: logging.warning("⚠️ Vertex AI setup skipped.")
except FileNotFoundError as e: logging.error(f"❌ {e}")
except Exception as e: logging.error(f"❌ SA credential/API init error: {e}")

# --- Load User Credentials (for Calendar) ---
try:
    if os.path.exists(TOKEN_PICKLE_FILE):
        with open(TOKEN_PICKLE_FILE, 'rb') as token: user_creds = pickle.load(token)
        logging.info(f"✅ User credentials loaded from {TOKEN_PICKLE_FILE}.")
    else: raise FileNotFoundError(f"User token file '{TOKEN_PICKLE_FILE}' not found. Run generate_token.py first.")
    # Refresh logic
    if user_creds and not user_creds.valid:
        if user_creds.expired and user_creds.refresh_token:
            logging.info("User credentials expired, refreshing...")
            try: # --- Outer Try for Refresh ---
                user_creds.refresh(Request())
                logging.info("User credentials refreshed successfully.")
                # --- Inner Try for Saving Refreshed Token ---
                try:
                    # Ensure 'try:' is on a new line and indented
                    with open(TOKEN_PICKLE_FILE, 'wb') as token:
                         pickle.dump(user_creds, token)
                    logging.info("✅ Refreshed user credentials saved.")
                # Ensure 'except' is aligned with its 'try'
                except Exception as e_save:
                     logging.error(f"❌ Error saving refreshed user credentials: {e_save}")
                     # Continue with refreshed creds in memory even if save fails
            # Ensure 'except' is aligned with its 'try'
            except Exception as e_refresh:
                 logging.error(f"❌ Error refreshing user credentials: {e_refresh}. Manual re-authentication via generate_token.py may be needed.")
                 user_creds = None # Invalidate creds if refresh fails
        else: logging.error(f"❌ User creds invalid, no refresh token. Run generate_token.py."); user_creds = None
    # Init Calendar Service
    if user_creds:
         if CALENDAR_ID:
             try: calendar_service = build("calendar", "v3", credentials=user_creds); _CALENDAR_INIT_SUCCESS = True; logging.info(f"✅ Calendar Service initialized (User Creds).")
             except Exception as e: logging.error(f"❌ Calendar Service init failed: {e}"); _CALENDAR_INIT_SUCCESS = False
         else: logging.error("❌ Calendar Service setup failed: CID missing/invalid."); _CALENDAR_INIT_SUCCESS = False
    else: logging.error("❌ Could not initialize Calendar Service: User creds invalid."); _CALENDAR_INIT_SUCCESS = False
except FileNotFoundError as e: logging.error(f"❌ {e}"); _CALENDAR_INIT_SUCCESS = False
except Exception as e: logging.error(f"❌ Error loading/refreshing user creds: {e}"); _CALENDAR_INIT_SUCCESS = False

# Init Twilio
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try: twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN); _TWILIO_INIT_SUCCESS = True; logging.info("✅ Twilio Client initialized.")
    except Exception as e: logging.error(f"❌ Twilio client init error: {e}"); _TWILIO_INIT_SUCCESS = False
else: logging.warning("⚠️ Twilio client setup skipped."); _TWILIO_INIT_SUCCESS = False

# --- Create Flask App & Attach Objects ---
app = Flask(__name__)
# Create static subfolder for audio cache
AUDIO_CACHE_DIR_NAME = 'audio_cache'
AUDIO_CACHE_FULL_PATH = os.path.join(app.static_folder, AUDIO_CACHE_DIR_NAME)
if not os.path.exists(AUDIO_CACHE_FULL_PATH):
    try: os.makedirs(AUDIO_CACHE_FULL_PATH); logging.info(f"Created audio cache directory: {AUDIO_CACHE_FULL_PATH}")
    except OSError as e: logging.error(f"❌ Failed to create audio cache directory '{AUDIO_CACHE_FULL_PATH}': {e}")

# Attach services
app.sheets_service = sheets_service if _SHEETS_INIT_SUCCESS else None
app.twilio_client = twilio_client if _TWILIO_INIT_SUCCESS else None
app.calendar_service = calendar_service if _CALENDAR_INIT_SUCCESS else None
app.gemini_model = gemini_model if _VERTEX_INIT_SUCCESS else None

# Attach config
app.config['SHEET_ID'] = SHEET_ID
app.config['CALENDAR_ID'] = CALENDAR_ID
app.config['TWILIO_PHONE_NUMBER'] = TWILIO_PHONE_NUMBER
app.config['PUBLIC_BASE_URL'] = PUBLIC_BASE_URL.rstrip('/') if PUBLIC_BASE_URL else None
app.config['CALL_CONTEXT_MAP'] = {}
app.config['COMPANY_INFO'] = COMPANY_INFO_TEXT
app.config['JOB_DESCRIPTION'] = JOB_DESCRIPTION_TEXT # Add loaded JD
app.config['ELEVENLABS_API_KEY'] = ELEVENLABS_API_KEY
app.config['ELEVENLABS_VOICE_ID'] = ELEVENLABS_VOICE_ID
app.config['AUDIO_CACHE_DIR_NAME'] = AUDIO_CACHE_DIR_NAME
app.config['AUDIO_CACHE_FULL_PATH'] = AUDIO_CACHE_FULL_PATH
logging.info("Services, model, config attached to Flask app.")

# --- Configure routes ---
from app.routes import configure_routes, get_candidate_to_call
try:
    configure_routes(app)
    logging.info("Flask routes configured.")
except Exception as e:
     logging.error(f"❌ Failed during configure_routes: {e}")
     sys.exit(1)

# --- Lock File Setup & run_one_time_call ---
LOCK_FILE_NAME = ".startup_call.lock"
def remove_lock_file():
    try:
        if os.path.exists(LOCK_FILE_NAME): os.remove(LOCK_FILE_NAME); logging.debug(f"Removed lock file.")
    except OSError as e: logging.error(f"Error removing lock file: {e}")
atexit.register(remove_lock_file) # Register cleanup function

def run_one_time_call():
    if not _SHEETS_INIT_SUCCESS or not _TWILIO_INIT_SUCCESS: logging.error("[Startup Call] Prerequisites failed."); return
    if os.path.exists(LOCK_FILE_NAME): logging.info(f"Lock file found. Skipping."); return
    lock_created = False
    try:
        with open(LOCK_FILE_NAME, 'w') as f: f.write(f"Locked"); lock_created = True
        logging.info(f"Created lock file: {LOCK_FILE_NAME}")
        logging.info("--- [Startup Call] Attempting call ---")
        local_sheets_service = sheets_service; local_twilio_client = twilio_client
        local_sheet_id = SHEET_ID; local_twilio_phone_number = TWILIO_PHONE_NUMBER
        with app.app_context():
             local_public_base_url = app.config.get('PUBLIC_BASE_URL')
             call_context_map = app.config.get('CALL_CONTEXT_MAP')
        if not local_public_base_url: logging.error("[Startup Call] PUBLIC_BASE_URL missing."); return
        # Use get_candidate_to_call (which no longer returns JD)
        candidate_data = get_candidate_to_call(local_sheets_service, local_sheet_id)
        if not candidate_data: logging.info("[Startup Call] No candidate 'To Call'."); return
        logging.info(f"[Startup Call] Found candidate: {candidate_data['name']}. Initiating call...")
        to_number = candidate_data['phone']; from_number = local_twilio_phone_number
        voice_url = f"{local_public_base_url}/voice"; status_callback_url = f"{local_public_base_url}/call-status"
        logging.info(f"[Startup Call] URLs: Voice={voice_url}, Status={status_callback_url}")
        try:
            call = local_twilio_client.calls.create(to=to_number, from_=from_number, url=voice_url, status_callback=status_callback_url, status_callback_event=['completed', 'no-answer', 'failed', 'busy', 'canceled'], status_callback_method='POST')
            logging.info(f"✅ [Startup Call] Call initiated. SID: {call.sid}")
            with app.app_context(): # Store context
                app.config['CALL_CONTEXT_MAP'][call.sid] = { k: v for k, v in candidate_data.items() }
            logging.info(f"Stored context for CallSid {call.sid}")
        except Exception as e: logging.error(f"❌ [Startup Call] Failed Twilio call create: {e}")
        logging.info("--- [Startup Call] Finished ---")
    except Exception as e_outer: logging.error(f"Error during locked execution: {e_outer}")
    finally:
        if lock_created: remove_lock_file()

# --- Main Execution Block ---
if __name__ == "__main__":
    remove_lock_file()
    if not _TWILIO_INIT_SUCCESS: logging.critical("❌ Twilio Client failed. EXITING."); sys.exit(1)
    if not _SHEETS_INIT_SUCCESS: logging.critical("❌ Sheets Service failed. EXITING."); sys.exit(1)
    if not _CALENDAR_INIT_SUCCESS: logging.warning("⚠️ Calendar Service failed. Booking will fail.")
    if not _VERTEX_INIT_SUCCESS: logging.warning("⚠️ Vertex AI failed. AI responses will fail.")

    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        logging.info("Werkzeug Reloader active - Running startup call.")
        if _SHEETS_INIT_SUCCESS and _TWILIO_INIT_SUCCESS: run_one_time_call()
        else: logging.warning("Skipping startup call due to failed Sheets/Twilio.")
    logging.info(f"--- Starting Flask server on host 0.0.0.0 port 5000 ---")
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=True)
    logging.info("--- Flask server stopped ---")
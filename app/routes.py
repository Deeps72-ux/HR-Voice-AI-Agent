# routes.py - FINAL Version (Booking Trigger Logs, Time Format Fix, JD Prompt, Final Q Flow)

import logging
import os
from flask import Blueprint, request, current_app, session, url_for, send_from_directory
from twilio.twiml.voice_response import VoiceResponse, Gather, Pause
import datetime
import pytz
from dateutil import parser, tz
import json
import html # Keep for fallback Say SSML
import re
import random
import threading
import uuid
import requests

try:
    from vertexai.generative_models import GenerativeModel, Part, HarmCategory, HarmBlockThreshold
    vertex_ai_available = True
    logging.info("Vertex AI SDK imported successfully.")
except ImportError:
    logging.warning("⚠️ Vertex AI SDK not found. AI features will be disabled.")
    vertex_ai_available = False

routes_bp = Blueprint("routes", __name__)

# --- Constants ---
LOG_FILE_NAME = "hr_agent_conversations.log"; COMPANY_NAME = "Panda Technologies"
AI_PERSONALITY = { "name": "Ayushi", "background": f"HR representative for {COMPANY_NAME}", "traits": "Professional, helpful, clear, concise.", "speaking_style": "Clear, professional English." }
INTERVIEW_DURATION_MINUTES = 45
IST = pytz.timezone('Asia/Kolkata')
ELEVENLABS_MODEL_ID = "eleven_multilingual_v2"
FALLBACK_VOICE = "Google.en-IN-Wavenet-A"; FALLBACK_LANG = "en-IN"

# --- State Constants ---
S_START = "START"
S_AWAIT_VERIFY = "AWAIT_VERIFY"
S_AWAIT_OK = "AWAIT_OK"
S_AWAIT_SLOT_CONFIRM = "AWAIT_SLOT_CONFIRM"
S_AWAIT_AVAILABILITY = "AWAIT_AVAILABILITY"
S_AWAIT_CALLBACK = "AWAIT_CALLBACK"
S_AWAIT_FINAL_Q = "AWAIT_FINAL_Q"
S_END = "END" # Terminal state

# --- Helper Functions ---

def enhance_ssml(text): # Only used for fallback <Say>
    if not text: return ""
    safe_text = html.escape(text); processed_text = safe_text
    processed_text = re.sub(r'\*(.+?)\*', r'<emphasis level="moderate">\1</emphasis>', processed_text)
    # Removed time interpretation for say-as, rely on formatting fix
    # processed_text = re.sub(r'(\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)\b)', r'<say-as interpret-as="time">\1</say-as>', processed_text)
    processed_text = re.sub(r'([.!?])\s+', r'\1<break time="450ms"/>', processed_text)
    processed_text = processed_text.replace(',', ',<break time="250ms"/>')
    final_ssml = f'<speak><prosody rate="95%">{processed_text}</prosody></speak>'
    return final_ssml

def get_candidate_to_call(sheets_service, sheet_id):
    # (Reads sheet, NO JD) - Unchanged
    if not sheets_service: logging.error("[get_candidate] Sheets service missing."); return None
    if not sheet_id: logging.error("[get_candidate] Sheet ID missing."); return None
    sheet_name='candidates'; range_to_read=f"{sheet_name}!A2:P"; status_col=8; status_find="To Call"
    name_idx=1; phone_idx=2; email_idx=3; title_idx=5; int_idx=11; slots_idx=12;
    try:
        logging.info(f"[get_candidate] Reading sheet: {sheet_id}, range: {range_to_read}")
        result = sheets_service.spreadsheets().values().get(spreadsheetId=sheet_id, range=range_to_read).execute()
        values = result.get('values', []);
        if not values: logging.info("[get_candidate] No data."); return None
        for index, row in enumerate(values):
            if len(row) > status_col and row[status_col] == status_find:
                row_num = index + 2; name = row[name_idx] if len(row) > name_idx else "N/A"; phone = row[phone_idx] if len(row) > phone_idx else "N/A"; title = row[title_idx] if len(row) > title_idx else "N/A"; email = row[email_idx] if len(row) > email_idx else "N/A"; slots = row[slots_idx] if len(row) > slots_idx else ""; int_email = row[int_idx] if len(row) > int_idx else ""
                logging.info(f"[get_candidate] Found '{name}' row {row_num}.")
                if phone=="N/A" or not str(phone).strip(): logging.warning(f"Skip {name}: missing phone."); continue
                if email=="N/A" or not str(email).strip() or '@' not in email: logging.warning(f"Skip {name}: missing/invalid email."); continue
                if int_email=="N/A" or not str(int_email).strip() or '@' not in int_email: logging.warning(f"Skip {name}: missing/invalid interviewer email."); continue
                return {'name':name, 'phone':str(phone).strip(), 'email':email, 'title':title, 'row_index':row_num, 'available_slots_str':slots, 'interviewer_email':int_email}
        logging.info(f"[get_candidate] No candidates '{status_find}'."); return None
    except Exception as e: logging.error(f"❌ [get_candidate] Sheet read error: {e}"); return None

def update_sheet_status(row_index, status, notes="", scheduled_time_str=""):
    # (Updates sheet) - Unchanged
    logging.debug(f"[update_sheet_status] Attempting update for Row: {row_index}, Status: '{status}', Notes: '{notes[:50]}...', Time: '{scheduled_time_str}'")
    sheets_service=getattr(current_app, 'sheets_service', None); sheet_id=current_app.config.get('SHEET_ID')
    if not sheets_service or not sheet_id: logging.error("[update] Sheets service/ID missing."); return False
    if not row_index: logging.error("[update] Missing row_index."); return False
    try:
        payload=[]; status_r=f"candidates!I{row_index}"; notes_r=f"candidates!J{row_index}"; time_r=f"candidates!N{row_index}"
        payload.append({'range': status_r, 'values': [[status or ""]]}); payload.append({'range': notes_r, 'values': [[notes or ""]]})
        if scheduled_time_str: payload.append({'range': time_r, 'values': [[scheduled_time_str]]})
        body = {'valueInputOption': 'USER_ENTERED', 'data': payload}; logging.info(f"[update] Row {row_index}: Updating status to '{status}'")
        result = sheets_service.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id, body=body).execute();
        total_updated = result.get('totalUpdatedCells', 0)
        logging.info(f"[update] Sheet update successful for row {row_index}. Result: {total_updated} cells."); return True
    except Exception as e:
        logging.error(f"❌ [update] Error updating sheet row {row_index} to status '{status}': {e}"); return False

def parse_iso_slots(slots_string, timezone_str='Asia/Kolkata'):
    # (Parses slots) - Unchanged
    if not slots_string: return []
    parsed_slots=[]; tzinfo=pytz.timezone(timezone_str); now=datetime.datetime.now(tzinfo)
    delimiters=[";",",","\n"]; slots=[]
    for d in delimiters:
        if d in slots_string: slots = [s.strip() for s in slots_string.split(d) if s.strip()]; break
    if not slots: slots = [slots_string.strip()] if slots_string.strip() else []
    for s in slots:
        try:
            dt=parser.parse(s)
            if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None: dt = tzinfo.localize(dt)
            else: dt = dt.astimezone(tzinfo)
            if dt > now: parsed_slots.append(dt)
            else: logging.warning(f"[parse_slots] Skip past slot: {s}")
        except Exception as e: logging.warning(f"[parse_slots] Parse fail for slot '{s}': {e}")
    return sorted(parsed_slots)

def format_iso_to_natural(iso_string):
    # (Formats datetime) - *** UPDATED to remove hour padding ***
    if not iso_string: return "the proposed time"
    try:
        dt=parser.parse(iso_string)
        local_tz=pytz.timezone('Asia/Kolkata')
        dt_local=dt.astimezone(local_tz)
        # Format hour without zero-padding for 12-hour clock
        hour_12 = int(dt_local.strftime('%I')) # Get hour as int
        # Construct format string using non-padded hour
        fmt = dt_local.strftime(f'%A, %B %d at {hour_12}:%M %p %Z')
        logging.debug(f"Formatted time '{iso_string}' to '{fmt}'")
        return fmt
    except Exception as e:
        logging.error(f"❌ Format ISO fail '{iso_string}': {e}");
        # Fallback in case of error
        try:
             dt=parser.parse(iso_string); return dt.strftime('%I:%M %p') # Simpler fallback
        except: return "the proposed time"


def create_calendar_event(start_dt, end_dt, summary, description, cand_email, int_email, sid):
    # (Creates event using User Creds) - Unchanged
    logging.debug(f"[{sid}] Attempting create_calendar_event. Start: {start_dt}, End: {end_dt}, Summary: '{summary}', CandEmail: {cand_email}, IntEmail: {int_email}")
    cal_svc=getattr(current_app, 'calendar_service', None); cal_id=current_app.config.get('CALENDAR_ID','primary')
    if not cal_svc: logging.error(f"[{sid}] Calendar service unavailable in create_calendar_event."); return None,None
    if not isinstance(start_dt, datetime.datetime) or not isinstance(end_dt, datetime.datetime): logging.error(f"[{sid}] Invalid datetime objects passed to create_calendar_event."); return None,None
    if not cand_email or '@' not in cand_email: logging.error(f"[{sid}] Invalid candidate email for calendar event: {cand_email}"); return None,None
    if not int_email or '@' not in int_email: logging.error(f"[{sid}] Invalid interviewer email for calendar event: {int_email}"); return None,None

    tz_str=str(start_dt.tzinfo) if start_dt.tzinfo else 'Asia/Kolkata'
    if not tz_str:
         logging.error(f"[{sid}] Failed to determine timezone string for calendar event.")
         return None, None

    body={'summary':summary, 'description':description, 'start':{'dateTime':start_dt.isoformat(), 'timeZone':tz_str}, 'end':{'dateTime':end_dt.isoformat(), 'timeZone':tz_str}, 'attendees':[{'email':cand_email},{'email':int_email}], 'conferenceData':{'createRequest':{'requestId':f"meet-{sid}-{random.randint(1000,9999)}",'conferenceSolutionKey':{'type':'hangoutsMeet'}}}, 'reminders':{'useDefault':False,'overrides':[{'method':'email','minutes':60},{'method':'popup','minutes':15}]}}
    logging.info(f"[{sid}] Calendar event request body prepared. Timezone: {tz_str}")

    try:
        logging.info(f"[{sid}] Executing Google Calendar API events().insert request (User Creds)...");
        event=cal_svc.events().insert(calendarId=cal_id, body=body, sendNotifications=True, conferenceDataVersion=1).execute();
        link=event.get('htmlLink'); meet=event.get('hangoutLink');
        logging.info(f"✅ [{sid}] Google Calendar event created successfully. Event ID: {event.get('id')}, Meet Link: {meet}");
        return link, meet
    except Exception as e:
        error_details = getattr(e, 'content', str(e))
        logging.error(f"❌ [{sid}] Error calling Google Calendar API (events.insert): {error_details}");
        return None,None

def background_calendar_booking(app_ctx, start, end, summary, desc, c_email, i_email, sid, row_idx, iso_time):
    # (Background task for booking) - Unchanged
    logging.info(f"--- [{sid}] Starting background booking task for Row {row_idx} at {iso_time} ---")
    status="Booking Failed"; notes=f"Background Task Error (Initial) for {iso_time}"
    link = None; meet = None; update_ok = False; cal_svc = None

    with app_ctx.app_context():
        logging.debug(f"[{sid} BG] Attempting to get Calendar Service...")
        cal_svc=getattr(current_app, 'calendar_service', None)
        if not cal_svc:
            logging.error(f"[{sid} BG] Calendar service unavailable inside background task context.")
            notes = "Internal Error: Calendar Service Unavailable"
        else:
             logging.info(f"[{sid} BG] Calendar Service retrieved successfully.")
             try:
                 logging.info(f"[{sid} BG] Calling create_calendar_event...")
                 link, meet = create_calendar_event(start, end, summary, desc, c_email, i_email, sid)
                 if link:
                     status="Scheduled"; notes=f"Scheduled via AI. Meet Link: {meet or 'N/A'}"; logging.info(f"✅ [{sid} BG] create_calendar_event successful.")
                 else:
                     notes=f"Calendar Event Creation Failed (Check logs for details) at {iso_time}"
                     logging.error(f"[{sid} BG] create_calendar_event returned None (failed).")
             except Exception as e_create:
                 logging.error(f"❌ [{sid} BG] Exception during create_calendar_event call: {e_create}")
                 notes=f"Calendar API Exception during creation: {e_create}"

        logging.info(f"[{sid} BG] Attempting sheet update. Status: '{status}', Notes: '{notes[:50]}...', Time: {iso_time}")
        try:
            update_ok = update_sheet_status(row_idx, status, notes, iso_time)
            if not update_ok:
                logging.error(f"❌ [{sid} BG] update_sheet_status FAILED. Status was '{status}'.")
                if status=="Scheduled":
                    logging.critical(f"[{sid} BG] CRITICAL ERROR: Calendar event created BUT sheet update failed for Row {row_idx}!")
            else:
                logging.info(f"✅ [{sid} BG] update_sheet_status successful. Final status for Row {row_idx}: '{status}'.")
        except Exception as e_update:
             logging.error(f"❌ [{sid} BG] Exception during update_sheet_status call: {e_update}")
             if status=="Scheduled":
                 logging.critical(f"[{sid} BG] CRITICAL ERROR: Calendar event created BUT sheet update failed due to exception for Row {row_idx}!")

    logging.info(f"--- [{sid}] Background booking task finished for Row {row_idx}. Final Status Attempted: '{status}' ---")


def generate_elevenlabs_audio(text, sid):
    # (Generates audio) - Unchanged
    key=current_app.config.get('ELEVENLABS_API_KEY'); vid=current_app.config.get('ELEVENLABS_VOICE_ID'); cache=current_app.config.get('AUDIO_CACHE_FULL_PATH')
    if not key or not vid or not cache: logging.error(f"[{sid}] ElevenLabs cfg missing."); return None
    if not text: logging.warning(f"[{sid}] Empty text to ElevenLabs."); return None
    url=f"https://api.elevenlabs.io/v1/text-to-speech/{vid}"; headers={"Accept":"audio/mpeg","Content-Type":"application/json","xi-api-key":key}
    data={"text":text,"model_id":ELEVENLABS_MODEL_ID,"voice_settings":{"stability":0.5,"similarity_boost":0.75}}; fname=f"{sid}_{uuid.uuid4()}.mp3"; fpath=os.path.join(cache,fname)
    try:
        logging.info(f"[{sid}] Requesting ElevenLabs TTS for text: '{text[:50]}...'")
        resp = requests.post(url, headers=headers, json=data, timeout=20)
        resp.raise_for_status()
        try:
            with open(fpath,"wb") as f:
                f.write(resp.content);
            logging.info(f"[{sid}] ElevenLabs audio saved successfully: {fname}"); return fname
        except Exception as e_save:
            logging.error(f"[{sid}] Error saving ElevenLabs audio file {fpath}: {e_save}")
            return None
    except requests.exceptions.RequestException as e:
        status_code = e.response.status_code if e.response is not None else 'N/A'
        logging.error(f"[{sid}] Error calling ElevenLabs API: Status {status_code}, Error: {e}")
        return None


@routes_bp.route('/audio/<path:filename>')
def serve_audio(filename):
    # (Serves audio) - Unchanged
    cache=current_app.config.get('AUDIO_CACHE_FULL_PATH')
    if not cache: return "Audio cache not configured", 500
    if '..' in filename or filename.startswith('/'):
         logging.warning(f"Invalid audio filename requested: {filename}")
         return "Invalid filename", 400
    try:
        logging.debug(f"Serving audio file: {filename}")
        return send_from_directory(cache, filename, mimetype='audio/mpeg')
    except FileNotFoundError:
        logging.warning(f"Audio file not found: {filename}")
        return "Audio resource not found", 404
    except Exception as e:
        logging.error(f"Error serving audio file {filename}: {e}")
        return "Internal server error", 500

# --- Gemini Processing Function (Updated S_AWAIT_FINAL_Q prompt logic) ---
def process_with_gemini(user_input, call_sid, call_context, current_state):
    """Processes input using Gemini, driven by explicit state."""
    gemini_model = getattr(current_app, 'gemini_model', None)
    company_info_text = current_app.config.get('COMPANY_INFO', '')
    job_description_text = current_app.config.get('JOB_DESCRIPTION', '') # Make sure job_description.txt content is useful!

    if not vertex_ai_available or not gemini_model:
        logging.warning(f"[{call_sid}] Vertex AI unavailable during processing.")
        return {"response_text":"Sorry, there's an internal issue with the AI connection. Please try calling back later.", "next_state":S_END, "needs_more_info":False, "hr_context":{}}

    history_key = f"{call_sid}_history"; conversation_history = session.get(history_key, [])
    candidate_name = call_context.get('name', 'Candidate'); first_name = candidate_name.split()[0] if candidate_name != "Candidate" else "there"
    job_title = call_context.get('title', 'the position'); available_slots_str = call_context.get('available_slots_str', '')

    try:
        parsed_slots = parse_iso_slots(available_slots_str)
        # Use the updated format function here
        formatted_slot_options = [format_iso_to_natural(dt.isoformat()) for dt in parsed_slots]
        slot_options_text = "; ".join(formatted_slot_options) if formatted_slot_options else "No specific times currently listed."
    except Exception as e_parse:
        logging.error(f"[{call_sid}] Error parsing/formatting slots '{available_slots_str}': {e_parse}")
        parsed_slots = []
        formatted_slot_options = []
        slot_options_text = "Error reading available times."

    # Get proposed slot from session IF it exists (might be confirmed now)
    proposed_or_confirmed_slot_iso = session.get(f"{call_sid}_proposed_slot")
    # If state is AWAIT_FINAL_Q, the relevant slot IS the one passed via hr_context previously
    # We need Gemini to use the 'proposed_slot_iso' variable passed IN THE PROMPT CONTEXT
    # when generating responses for S_AWAIT_FINAL_Q (it represents the confirmed slot)

    last_assistant_utterance = conversation_history[-2]['content'] if len(conversation_history) >= 2 and conversation_history[-1]['role'] == 'user' and conversation_history[-2]['role'] == 'assistant' else None

    # --- Build the State-Driven Prompt ---
    # *** UPDATED S_AWAIT_FINAL_Q logic significantly ***
    prompt = f"""
    You are {AI_PERSONALITY["name"]}, an HR representative from {COMPANY_NAME}. Goal: Schedule interview for {job_title} with {candidate_name}. Be professional, clear, concise. Do NOT identify as AI.

    CANDIDATE CONTEXT: Name: {candidate_name} (Address as {first_name}) | Job: {job_title}
    COMPANY INFO: {company_info_text if company_info_text.strip() else "N/A"}
    JOB DESC: {job_description_text if job_description_text.strip() else "N/A"}
    AVAILABLE SLOTS: {slot_options_text}
    PROPOSED/CONFIRMED SLOT ISO: {proposed_or_confirmed_slot_iso if proposed_or_confirmed_slot_iso else "None"} # This IS the confirmed slot in state {S_AWAIT_FINAL_Q}
    CURRENT STATE: {current_state}
    HISTORY (Last few): {json.dumps(conversation_history[-6:], indent=1)}
    LAST AI UTTERANCE: {json.dumps(last_assistant_utterance)}
    USER INPUT: {json.dumps(user_input) if user_input else "(No user input)"}

    YOUR TASK: Based ONLY on CURRENT STATE and USER INPUT, decide NEXT STATE and generate RESPONSE TEXT according to the STATE LOGIC below.

    STATE LOGIC:
    - IF STATE '{S_START}': Greet & verify. RESPONSE: "Hi, this is {AI_PERSONALITY["name"]} calling from the HR department at {COMPANY_NAME}. Am I speaking with {first_name}?". NEXT STATE: '{S_AWAIT_VERIFY}'. needs_more_info: true. hr_context:{{}}
    - IF STATE '{S_AWAIT_VERIFY}':
        - If USER INPUT confirms ("Yes", "Speaking"): RESPONSE: "Great. Calling about your application for {job_title}. Is now an okay time to discuss scheduling for a minute?". NEXT STATE: '{S_AWAIT_OK}'. needs_more_info: true. hr_context:{{}}
        - If USER INPUT denies ("No", "Wrong number"): RESPONSE: "Oh, I apologize. Goodbye.". NEXT STATE: '{S_END}'. needs_more_info: false. hr_context:{{}}
        - If USER INPUT unclear: RESPONSE: "Sorry, is this {first_name}?". NEXT STATE: '{S_AWAIT_VERIFY}'. needs_more_info: true. hr_context:{{}}
    - IF STATE '{S_AWAIT_OK}':
        - If USER INPUT confirms ("Yes", "Sure", "Okay", "Good time"): Check AVAILABLE SLOTS.
            - If slots exist: Acknowledge ("Okay, great."). Propose first slot ("Are you available on {formatted_slot_options[0] if formatted_slot_options else 'one of the listed times'}?"). NEXT STATE: '{S_AWAIT_SLOT_CONFIRM}'. needs_more_info: true. hr_context: {{"proposed_slot_iso": "{parsed_slots[0].isoformat() if parsed_slots else ''}"}}
            - If NO slots: Acknowledge ("Okay, great."). Ask general availability ("Could you let me know what days or times might work for a {INTERVIEW_DURATION_MINUTES} minute interview?"). NEXT STATE: '{S_AWAIT_AVAILABILITY}'. needs_more_info: true. hr_context:{{}}
        - If USER INPUT negative ("No", "Busy"): RESPONSE: "Okay, no problem. When might be a better time for me to call you back briefly?". NEXT STATE: '{S_AWAIT_CALLBACK}'. needs_more_info: true. hr_context:{{}}
        - If USER INPUT unclear: RESPONSE: "Sorry, is now a good time to talk briefly about scheduling?". NEXT STATE: '{S_AWAIT_OK}'. needs_more_info: true. hr_context:{{}}
    - IF STATE '{S_AWAIT_SLOT_CONFIRM}':
        - If USER INPUT affirms ("Yes", "Okay", "That works"): RESPONSE: "Okay, confirmed! I have scheduled you for {format_iso_to_natural(proposed_or_confirmed_slot_iso)}. You should receive a calendar invite shortly. Do you have any quick questions before we go?". NEXT STATE: '{S_AWAIT_FINAL_Q}'. needs_more_info: true. Output hr_context: {{"confirmed_slot_iso": "{proposed_or_confirmed_slot_iso}"}}. # Signal booking
        - If USER INPUT negative ("No", "doesn't work"): Acknowledge ("Okay, that time doesn't work."). Check other slots. Find index of proposed_or_confirmed_slot_iso in parsed_slots. If next slot exists (index+1 < len(parsed_slots)): Propose next slot ("Understood. Alternatively, how about {formatted_slot_options[parsed_slots.index(parser.parse(proposed_or_confirmed_slot_iso)) + 1 if proposed_or_confirmed_slot_iso and parser.parse(proposed_or_confirmed_slot_iso) in parsed_slots and parsed_slots.index(parser.parse(proposed_or_confirmed_slot_iso)) + 1 < len(parsed_slots) else 1]}?"). NEXT STATE: '{S_AWAIT_SLOT_CONFIRM}'. needs_more_info: true. Store *new* proposed ISO ({parsed_slots[parsed_slots.index(parser.parse(proposed_or_confirmed_slot_iso)) + 1].isoformat() if proposed_or_confirmed_slot_iso and parser.parse(proposed_or_confirmed_slot_iso) in parsed_slots and parsed_slots.index(parser.parse(proposed_or_confirmed_slot_iso)) + 1 < len(parsed_slots) else ''}). hr_context: {{"proposed_slot_iso": ...}}
        - If USER INPUT negative AND no other slots: Ask general availability ("Okay. It looks like that was the last listed option for now. What days or times generally work better for you?"). NEXT STATE: '{S_AWAIT_AVAILABILITY}'. needs_more_info: true. hr_context: {{}}
        - If USER INPUT asks question about job/company: Answer *concisely* using ONLY JOB DESC/COMPANY INFO. If info not available, state "I don't have those specific details, but the hiring manager can answer that during the interview." After answering/deflecting, *immediately* re-ask about the *same* proposed slot: "Regarding the time {format_iso_to_natural(proposed_or_confirmed_slot_iso)}, does that still work for you?". NEXT STATE: '{S_AWAIT_SLOT_CONFIRM}'. needs_more_info: true. hr_context: {{"proposed_slot_iso": "{proposed_or_confirmed_slot_iso}"}}
        - If USER INPUT unclear: RESPONSE: "Sorry, I didn't quite catch that. Regarding the proposed time of {format_iso_to_natural(proposed_or_confirmed_slot_iso)}, does that work for you?". NEXT STATE: '{S_AWAIT_SLOT_CONFIRM}'. needs_more_info: true. hr_context: {{"proposed_slot_iso": "{proposed_or_confirmed_slot_iso}"}}
    - IF STATE '{S_AWAIT_AVAILABILITY}': RESPONSE: "Okay, thank you for letting me know. I've made a note of your availability preference. We will reach out separately if a matching time opens up. Have a great day!". NEXT STATE: '{S_END}'. needs_more_info: false. hr_context:{{}}
    - IF STATE '{S_AWAIT_CALLBACK}': RESPONSE: "Understood. Thank you for letting me know. Have a great day! Goodbye.". NEXT STATE: '{S_END}'. needs_more_info: false. hr_context:{{}}

    - IF STATE '{S_AWAIT_FINAL_Q}': # AI has just asked "Do you have any quick questions?" or "Do you have any other questions?"
        - If USER INPUT asks a question (e.g., "What is the format?", "Tell me about X"):
            # Step 1: Answer the question.
            RESPONSE PART 1: Search the JOB DESC and COMPANY INFO context *thoroughly* for the answer. If the specific information is found, provide it concisely. If the specific information is genuinely not present in the provided context, state: "That specific detail isn't listed here, but the hiring manager can clarify during the interview."
            # Step 2: Ask if there are more questions.
            RESPONSE PART 2: " Do you have any other questions?"
            # Combine parts for the final response.
            RESPONSE: "[RESPONSE PART 1] Do you have any other questions?"
            NEXT STATE: '{S_AWAIT_FINAL_Q}'. needs_more_info: true. hr_context:{{}} # Stay in this state.

        - ELSE IF USER INPUT confirms no more questions ("No", "Nope", "I'm good", "That's all"):
            RESPONSE: "Okay, great! We look forward to the interview at {format_iso_to_natural(proposed_or_confirmed_slot_iso)}. Have a wonderful day. Goodbye.".
            NEXT STATE: '{S_END}'. needs_more_info: false. hr_context:{{}} # End the call.

        - ELSE (User input is unclear):
            RESPONSE: "Sorry, I didn't quite understand. Did you have another question?".
            NEXT STATE: '{S_AWAIT_FINAL_Q}'. needs_more_info: true. hr_context:{{}} # Stay in this state to clarify.

    - Default/Fallback: RESPONSE: "Sorry, I missed that. Could you please repeat?". NEXT STATE: '{current_state}'. needs_more_info: true. hr_context:{{}} # Stay in current state

    Output ONLY valid JSON following the structure: {{"response_text": "...", "next_state": "...", "needs_more_info": boolean, "hr_context": {{...}} }} Ensure "next_state" is always included. Ensure "response_text" matches the logic precisely. Ensure hr_context is appropriate for the next state.
    """
    # Outer try for the entire Gemini interaction and processing
    try:
        safety_settings={HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE, HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE, HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE, HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE}
        generation_config={"temperature": 0.5, "response_mime_type": "application/json"}
        logging.debug(f"[{call_sid}] Sending prompt (State: {current_state}). History length: {len(conversation_history)}");

        if not hasattr(gemini_model, 'generate_content'):
            raise TypeError("Gemini model object is invalid or not initialized properly.")

        # logging.debug(f"[{call_sid}] Prompt sent to Gemini:\n{prompt}") # Uncomment for deep debugging

        gemini_response = gemini_model.generate_content(prompt, generation_config=generation_config, safety_settings=safety_settings)

        response_text_raw = ""; result = {}; json_string = "";

        try: # Extract response text
            if gemini_response.candidates and gemini_response.candidates[0].content.parts:
                 response_text_raw = gemini_response.candidates[0].content.parts[0].text
            else:
                 try:
                     response_text_raw = gemini_response.text
                     logging.warning(f"[{call_sid}] Gemini response structure unexpected, falling back to .text")
                 except Exception as e_text_fallback:
                     logging.error(f"[{call_sid}] Failed to extract text from Gemini response: {e_text_fallback}. Response: {gemini_response}")
                     raise ValueError("Blocked or No Text content found in Gemini response")
            if not response_text_raw:
                 safety_feedback = getattr(gemini_response, 'prompt_feedback', getattr(gemini_response, 'candidates', [{}])[0].get('safety_ratings', None))
                 logging.warning(f"[{call_sid}] Empty text received from Gemini. Safety Feedback: {safety_feedback}")
                 raise ValueError("Empty text received from Gemini")
        except Exception as e_text: # Handle extraction errors / blocks
            logging.error(f"❌ [{call_sid}] Error extracting text part from Gemini: {e_text}")
            safety_feedback = getattr(gemini_response, 'prompt_feedback', None)
            block_reason = getattr(safety_feedback, 'block_reason', None)
            if block_reason:
                logging.error(f"[{call_sid}] Gemini content blocked. Reason: {block_reason}")
                raise ValueError(f"Content blocked by Gemini safety filters. Reason: {block_reason}")
            else:
                 raise ValueError("Blocked/No Text: Failed to get valid text from Gemini response.")

        # Parse JSON response
        logging.debug(f"[{call_sid}] Raw Gemini JSON: {response_text_raw}");
        json_string = response_text_raw.strip()
        if json_string.startswith("```json"): # Basic cleanup
            match=re.search(r"```json\s*(.*?)\s*```",json_string,re.DOTALL);
            if match: json_string=match.group(1).strip()
            else: json_string = json_string.replace("```json", "").replace("```", "").strip()

        try: # Parse JSON
            result=json.loads(json_string);
            # *** Use INFO level for this crucial log for now ***
            logging.info(f"[{call_sid}] Gemini Parsed. Keys: {list(result.keys())}. Next state: {result.get('next_state')}. hr_context: {result.get('hr_context')}")
        except json.JSONDecodeError as e_json:
            logging.error(f"❌ [{call_sid}] Gemini JSON decode fail: {e_json}\nRaw: '{json_string}'")
            if not json_string.startswith('{'):
                logging.warning(f"[{call_sid}] Gemini output wasn't JSON. Using raw text as response, setting state to END.")
                result = {"response_text": json_string, "next_state": S_END, "needs_more_info": False, "hr_context": {}}
            else:
                 raise ValueError("Bad JSON: Failed to decode JSON from Gemini.")
        except Exception as e_parse:
             logging.error(f"❌ [{call_sid}] Unexpected error parsing Gemini JSON: {e_parse}\nRaw: '{json_string}'")
             raise ValueError(f"Bad JSON: Unexpected error {e_parse}")

        # Validate result structure
        if not all(k in result for k in ["response_text","next_state","needs_more_info"]):
            logging.error(f"❌ [{call_sid}] Gemini JSON missing required keys. Received: {result}")
            raise ValueError("Missing keys: Gemini JSON response missing required fields.")

        result.setdefault('hr_context',{}) # Ensure hr_context exists

        # Add AI response to history
        current_history=session.get(history_key,[]);
        current_history.append({"role":"assistant","content":result["response_text"]});
        session[history_key]=current_history[-10:]

        # Manage proposed/confirmed slot in session based on Gemini's output
        hr_ctx=result.get('hr_context',{});
        prop_iso_from_gemini=hr_ctx.get('proposed_slot_iso');
        conf_iso_from_gemini=hr_ctx.get('confirmed_slot_iso')

        if prop_iso_from_gemini:
            session[f"{call_sid}_proposed_slot"] = prop_iso_from_gemini;
            logging.debug(f"[{call_sid}] Stored proposed slot from Gemini hr_context: {prop_iso_from_gemini}")
        elif conf_iso_from_gemini or result.get('next_state') in [S_AWAIT_AVAILABILITY, S_AWAIT_CALLBACK, S_END]: # Clear if confirmed or moving away
            # Check if proposed slot exists before popping
            if session.get(f"{call_sid}_proposed_slot"):
                # Store confirmed slot before popping proposed (if applicable)
                if conf_iso_from_gemini:
                     session[f"{call_sid}_confirmed_slot"] = conf_iso_from_gemini # Store confirmed slot maybe? Needed?
                session.pop(f"{call_sid}_proposed_slot", None);
                logging.debug(f"[{call_sid}] Cleared proposed slot session key (Confirmed: {bool(conf_iso_from_gemini)}, State: {result.get('next_state')}).")
        # Note: S_AWAIT_FINAL_Q state now keeps proposed_slot in session to display confirmed time

        return result

    except Exception as e:
        logging.exception(f"❌ Unhandled Error in process_with_gemini (State: {current_state})", exc_info=e)
        return {"response_text": "I'm sorry, I encountered an unexpected internal error. Please try calling back later.", "next_state": S_END, "needs_more_info": False, "hr_context": {}}


# --- Route /voice (Initializes State) ---
@routes_bp.route("/voice", methods=['POST'])
def voice_handler():
    # (Initializes state to S_START, calls Gemini, uses ElevenLabs) - Unchanged
    resp=VoiceResponse(); sid=request.values.get('CallSid','Unknown'); dir=request.values.get('Direction','inbound');
    logging.info(f"[/voice] Incoming call. SID: {sid}, Direction: {dir}");
    ctx_map=current_app.config.get('CALL_CONTEXT_MAP',{}); ctx=None; hist_key=f"{sid}_history"; state_key=f"{sid}_state"

    if dir=='outbound-api':
        ctx=ctx_map.get(sid);
        if not ctx:
             logging.error(f"CRITICAL: Outbound call context missing for {sid}. Check startup call logic.");
             resp.say("There was an error initiating this call. Goodbye.", voice=FALLBACK_VOICE, language=FALLBACK_LANG); resp.hangup(); return str(resp)
    elif dir == 'inbound':
         logging.info(f"[{sid}] Inbound call received from {request.values.get('From', 'Unknown')}. Using default context.")
         ctx = {'name': 'Caller', 'title': 'the position', 'email': '', 'row_index': None, 'available_slots_str': '', 'interviewer_email': ''}
         ctx_map[sid] = ctx
    else:
        logging.warning(f"[{sid}] Unexpected call direction '{dir}'.")
        ctx = {}

    session[state_key]=S_START;
    session[hist_key]=[]
    logging.info(f"[{sid}] State initialized: {S_START}")

    ai_text="Hello."; audio_url=None; next_state=S_END; needs_more=False

    try:
        ai_result=process_with_gemini(None, sid, ctx, S_START);
        ai_text=ai_result.get("response_text", "Sorry, an error occurred.")
        next_state=ai_result.get("next_state", S_END)
        needs_more=ai_result.get("needs_more_info", False)

        session[state_key]=next_state
        session[hist_key]=[{"role":"assistant","content":ai_text}]
        logging.info(f"[/voice] Initial Gemini response OK. Next state: {next_state}")

    except Exception as e:
        logging.exception(f"[/voice] Error during initial Gemini processing for {sid}", exc_info=e);
        ai_text="I'm sorry, there was a problem starting our conversation. Goodbye."
        next_state=S_END
        needs_more=False
        session[state_key]=S_END

    try:
        if ai_text:
            audio_fname = generate_elevenlabs_audio(ai_text, sid)
            if audio_fname:
                base=current_app.config.get('PUBLIC_BASE_URL');
                if base:
                    try:
                         audio_url = url_for('routes.serve_audio', filename=audio_fname, _external=True)
                         logging.info(f"[{sid}] Generated audio URL: {audio_url}")
                    except Exception as url_e:
                         logging.error(f"[{sid}] Error generating audio URL with url_for: {url_e}")
                else:
                     logging.error(f"[{sid}] PUBLIC_BASE_URL not configured. Cannot create audio URL.")
            if not audio_url and ai_text:
                 logging.error(f"[{sid}] Failed to generate or get public URL for audio.")
    except Exception as e:
        logging.exception(f"[{sid}] Error during TTS generation/URL creation", exc_info=e)

    try:
        if needs_more and next_state != S_END:
            gather=Gather(input="speech",action=url_for('routes.process_voice', call_sid=sid), method="POST", timeout=5, speechTimeout='auto', speechModel="phone_call", enhanced=True, language=FALLBACK_LANG)
            if audio_url:
                gather.play(audio_url); logging.debug(f"[{sid}] Using <Play> for initial gather.")
            else:
                logging.warning(f"[{sid}] Fallback <Say> for initial gather.");
                ssml_text = enhance_ssml(ai_text) if '<' not in ai_text else ai_text
                gather.say(ssml_text, voice=FALLBACK_VOICE, language=FALLBACK_LANG)
            resp.append(gather)
            resp.redirect(url_for('routes.reprompt_stub', call_sid=sid))
        else:
             if audio_url:
                 resp.play(audio_url); logging.debug(f"[{sid}] Playing final message (initial state).")
             else:
                 logging.warning(f"[{sid}] Fallback <Say> for final message (initial state).")
                 ssml_text = enhance_ssml(ai_text) if '<' not in ai_text else ai_text
                 resp.say(ssml_text, voice=FALLBACK_VOICE, language=FALLBACK_LANG)
             resp.pause(length=1)
             resp.hangup()
             logging.info(f"Ending call {sid} immediately after initial response (State: {next_state}).")
             cleanup_call_data(sid)

    except Exception as e:
        logging.exception(f"[/voice] TwiML build error for {sid}", exc_info=e);
        resp = VoiceResponse()
        resp.say("A configuration error occurred. Please try again later.", voice=FALLBACK_VOICE, language=FALLBACK_LANG);
        resp.hangup()
        cleanup_call_data(sid)

    return str(resp)


# --- Route /process-voice (Manages State Transitions) ---
@routes_bp.route("/process-voice", methods=['POST'])
def process_voice():
    """ Processes subsequent user speech based on current state, uses ElevenLabs, handles booking. """
    sid=request.args.get('call_sid', request.values.get('CallSid','Unknown SID'))
    input_text=request.form.get("SpeechResult","").strip();
    conf=request.form.get("Confidence","N/A");
    resp=VoiceResponse();
    logging.info(f"[/process] SID:{sid}, Input:'{input_text}', Confidence:{conf}")

    ctx_map=current_app.config.get('CALL_CONTEXT_MAP',{}); ctx=ctx_map.get(sid);
    hist_key=f"{sid}_history"; state_key=f"{sid}_state"

    current_state=session.get(state_key, S_END);
    history=session.get(hist_key, [])

    if ctx is None and current_state != S_END:
        logging.error(f"CRITICAL: Context lost for active call {sid} (State: {current_state}). Ending call.");
        resp.say("I seem to have lost our context. Apologies, goodbye.", voice=FALLBACK_VOICE, language=FALLBACK_LANG);
        resp.hangup()
        cleanup_call_data(sid)
        return str(resp)
    elif ctx is None:
        logging.warning(f"[{sid}] Processing request for already ended/cleaned up call state. Ignoring further processing.");
        resp.hangup()
        return str(resp)

    logging.info(f"[{sid}] Current State: {current_state}")

    if not input_text:
        logging.warning(f"[{sid}] Empty speech input received. Reprompting.");
        ai_text="Sorry, I didn't catch that. Could you say it again?"
        audio_url=None
        try:
            fname=generate_elevenlabs_audio(ai_text,sid);
            if fname:
                 base=current_app.config.get('PUBLIC_BASE_URL')
                 if base: audio_url = url_for('routes.serve_audio', filename=fname, _external=True)
        except Exception as e: logging.error(f"[{sid}] Reprompt audio generation error: {e}")

        gather=Gather(input="speech",action=url_for('routes.process_voice', call_sid=sid), method="POST", timeout=4, speechModel="phone_call", enhanced=True, language=FALLBACK_LANG);
        if audio_url:
             gather.play(audio_url)
        else:
             ssml_text = enhance_ssml(ai_text) if '<' not in ai_text else ai_text
             gather.say(ssml_text, voice=FALLBACK_VOICE, language=FALLBACK_LANG)
        resp.append(gather);
        resp.redirect(url_for('routes.reprompt_stub', call_sid=sid));
        return str(resp)

    history.append({"role":"user","content":input_text});
    session[hist_key]=history[-10:]

    # --- Process with Gemini ---
    ai_text = "Sorry, an error occurred."; needs_more = False; next_state = S_END; hr_ctx = {}
    try:
        ai_result=process_with_gemini(input_text, sid, ctx, current_state);
        ai_text=ai_result.get("response_text", ai_text)
        needs_more=ai_result.get("needs_more_info", needs_more)
        next_state=ai_result.get("next_state", next_state)
        hr_ctx=ai_result.get("hr_context", hr_ctx)

        session[state_key]=next_state
        logging.info(f"[{sid}] Gemini processing complete. Determined next state: {next_state}") # Log state determination

    except Exception as e:
        logging.exception(f"[{sid}] Error during Gemini processing in /process-voice", exc_info=e)
        ai_text="I encountered an issue processing your response. Apologies. Goodbye."
        next_state=S_END
        needs_more=False
        session[state_key]=S_END
        hr_ctx={}

    # --- Generate audio ---
    audio_url=None
    if ai_text:
        try:
            fname=generate_elevenlabs_audio(ai_text,sid);
            if fname:
                 base=current_app.config.get('PUBLIC_BASE_URL')
                 if base: audio_url = url_for('routes.serve_audio', filename=fname, _external=True)
                 else: logging.error(f"[{sid}] Cannot create audio URL - PUBLIC_BASE_URL missing.")
            if not audio_url: logging.error(f"[{sid}] Failed to generate/get URL for response audio.")
        except Exception as e: logging.exception(f"[{sid}] Response audio generation error", exc_info=e)

    # --- Handle Booking Trigger ---
    conf_iso_from_gemini = hr_ctx.get("confirmed_slot_iso")

    # *** ADDED LOGGING FOR TRIGGER CONDITION ***
    logging.info(f"[{sid}] Checking booking trigger: Has Confirmed ISO from Gemini? {bool(conf_iso_from_gemini)}. Next State is AWAIT_FINAL_Q? {next_state == S_AWAIT_FINAL_Q}.")

    if conf_iso_from_gemini and next_state == S_AWAIT_FINAL_Q:
        # *** ADDED LOGGING INSIDE TRIGGER BLOCK ***
        logging.info(f"✅ [{sid}] Booking trigger condition MET. Confirmed ISO: {conf_iso_from_gemini}. Attempting to start background task.")
        try:
            start=parser.parse(conf_iso_from_gemini);
            tz_info=IST
            start=start.astimezone(tz_info) if start.tzinfo else tz_info.localize(start)
            end=start+datetime.timedelta(minutes=INTERVIEW_DURATION_MINUTES);
            summary=f"Interview: {ctx.get('name','Candidate')} - {ctx.get('title','Position')}"
            desc=f"""AI Scheduled Interview for {ctx.get('title','the position')}.

Candidate: {ctx.get('name','N/A')}
Candidate Email: {ctx.get('email','N/A')}
Interviewer Email: {ctx.get('interviewer_email','N/A')}

Call SID: {sid}
Scheduled Time: {start.strftime('%Y-%m-%d %H:%M:%S %Z')}
"""
            c_email=ctx.get('email'); i_email=ctx.get('interviewer_email'); row_idx=ctx.get('row_index')

            logging.debug(f"[{sid}] Booking thread parameters prepared.")
            logging.debug(f"  Start DT: {start}, End DT: {end}, Summary: {summary}")
            logging.debug(f"  Cand Email: {c_email}, Int Email: {i_email}, Row Index: {row_idx}")

            if c_email and i_email and row_idx:
                app_ctx=current_app._get_current_object();
                thread=threading.Thread(
                    target=background_calendar_booking,
                    args=(app_ctx,start,end,summary,desc,c_email,i_email,sid,row_idx,conf_iso_from_gemini),
                    daemon=True
                )
                thread.start();
                logging.info(f"✅ [{sid}] Dispatched background booking thread for row {row_idx}.")
            else:
                logging.error(f"❌ [{sid}] Cannot trigger booking: Missing details! Cand Email: {bool(c_email)}, Int Email: {bool(i_email)}, Row Index: {bool(row_idx)}")

        except Exception as e:
            logging.exception(f"❌ [{sid}] Error processing confirmed slot or dispatching booking thread", exc_info=e)
    elif next_state == S_AWAIT_FINAL_Q:
         logging.warning(f"[{sid}] Reached AWAIT_FINAL_Q state but 'confirmed_slot_iso' was missing in Gemini hr_context. Booking thread NOT started.")
    # --- End Booking Trigger ---

    # --- Build TwiML Response ---
    try:
        if needs_more and next_state != S_END:
            gather=Gather(input="speech",action=url_for('routes.process_voice', call_sid=sid), method="POST", timeout=5, speechModel="phone_call", enhanced=True, language=FALLBACK_LANG)
            if audio_url:
                gather.play(audio_url)
            else:
                ssml_text = enhance_ssml(ai_text) if '<' not in ai_text else ai_text
                gather.say(ssml_text, voice=FALLBACK_VOICE, language=FALLBACK_LANG)
            resp.append(gather)
            resp.redirect(url_for('routes.reprompt_stub', call_sid=sid))
        else: # End the call
            if audio_url:
                resp.play(audio_url)
            else:
                ssml_text = enhance_ssml(ai_text) if '<' not in ai_text else ai_text
                resp.say(ssml_text, voice=FALLBACK_VOICE, language=FALLBACK_LANG)
            resp.pause(length=1);
            resp.hangup();
            logging.info(f"Ending call {sid}: needs_more={needs_more}, next_state='{next_state}'.")
            cleanup_call_data(sid)

    except Exception as e:
        logging.exception(f"[{sid}] TwiML build error in /process-voice", exc_info=e)
        resp = VoiceResponse()
        resp.say("An internal error occurred. Goodbye.", voice=FALLBACK_VOICE, language=FALLBACK_LANG)
        resp.hangup()
        cleanup_call_data(sid)

    # --- Log conversation turn ---
    # Ensures logging happens even if subsequent steps fail
    log_entry = {"ts":datetime.datetime.now(IST).isoformat(),"sid":sid,"in":input_text,"conf":conf,"state":current_state,"next":next_state,"more":needs_more,"out":ai_text}
    try:
        with open(LOG_FILE_NAME,"a",encoding="utf-8") as f:
            f.write(json.dumps(log_entry)+"\n")
    except Exception as e: logging.error(f"❌ [{sid}] Log write fail: {e}")

    return str(resp)


# --- Helper Function for Cleanup ---
def cleanup_call_data(call_sid):
    """Removes call-specific data from session and context map."""
    # Unchanged
    if not call_sid or call_sid in ('Unknown', 'Unknown SID'):
         logging.warning("[Cleanup] Invalid CallSid provided for cleanup.")
         return

    logging.info(f"[Cleanup] Attempting cleanup for CallSid: {call_sid}")
    ctx_map = current_app.config.get('CALL_CONTEXT_MAP', {})
    hist_key=f"{call_sid}_history"; prop_key=f"{call_sid}_proposed_slot"; state_key=f"{call_sid}_state"

    if ctx_map.pop(call_sid, None) is not None:
        logging.debug(f"[Cleanup] Removed context for {call_sid}.")
    else:
        logging.debug(f"[Cleanup] Context for {call_sid} already removed or never existed.")

    if session:
        if session.pop(hist_key, None) is not None: logging.debug(f"[Cleanup] Removed history {hist_key}.")
        else: logging.debug(f"[Cleanup] History key {hist_key} not found in session.")

        if session.pop(prop_key, None) is not None: logging.debug(f"[Cleanup] Removed proposed slot {prop_key}.")
        else: logging.debug(f"[Cleanup] Proposed slot key {prop_key} not found in session.")

        if session.pop(state_key, None) is not None: logging.debug(f"[Cleanup] Removed state {state_key}.")
        else: logging.debug(f"[Cleanup] State key {state_key} not found in session.")
    else:
        logging.warning(f"[Cleanup] Session not available during cleanup for {call_sid}.")


# --- Route /call-status (Handles final call status from Twilio) ---
@routes_bp.route('/call-status', methods=['POST'])
def call_status():
    # Unchanged
    sid=request.form.get('CallSid'); status=request.form.get('CallStatus');
    to=request.form.get('To'); duration=request.form.get('CallDuration', 'N/A');
    logging.info(f"[/status] SID:{sid} Status:'{status}' To:{to} Duration:{duration}s")

    ctx_map=current_app.config.get('CALL_CONTEXT_MAP',{});
    ctx=ctx_map.get(sid)

    if ctx:
        row_idx=ctx.get('row_index')
        if row_idx:
            new_stat=""; notes=""
            if status in ['no-answer','failed','busy','canceled']:
                new_stat="No Answer / Failed";
                notes=f"Final Call Status: {status} ({datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M')})"
                try:
                    logging.info(f"[{sid}] Updating sheet via /call-status due to status: {status}")
                    update_sheet_status(row_idx, new_stat, notes)
                except Exception as e:
                     logging.error(f"[{sid}] Failed to update sheet on call status '{status}': {e}")
            elif status == 'completed':
                 logging.info(f"[{sid}] Call 'completed'. Sheet update handled by call flow/booking task.")
            else:
                logging.warning(f"[{sid}] Unhandled call status '{status}' in /call-status webhook.")
        else:
            logging.warning(f"[{sid}] Row index missing in context during /call-status processing.")
    else:
         logging.info(f"[{sid}] Context already cleaned up for status '{status}'. No action needed by webhook.")

    cleanup_call_data(sid)
    return '',200

# --- Route /reprompt (Handles Gather timeout) ---
@routes_bp.route("/reprompt", methods=['POST'])
def reprompt_stub():
    # Unchanged
    sid = request.args.get('call_sid', request.values.get('CallSid', 'Unknown SID'))
    logging.warning(f"[/reprompt] Timeout or empty input for SID: {sid}. Hanging up.")

    resp=VoiceResponse();
    resp.say("Sorry, I didn't hear anything. Please call back if you'd like to continue. Goodbye.", voice=FALLBACK_VOICE, language=FALLBACK_LANG);
    resp.hangup()

    ctx_map=current_app.config.get('CALL_CONTEXT_MAP',{});
    ctx=ctx_map.get(sid)
    if ctx:
        row_idx=ctx.get('row_index')
        if row_idx:
            logging.info(f"[{sid}] Updating sheet row {row_idx} due to timeout/reprompt.")
            notes=f"Call ended due to timeout/no response ({datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M')})"
            try:
                update_sheet_status(row_idx, "No Answer / Failed", notes)
            except Exception as e:
                 logging.error(f"[{sid}] Failed to update sheet on reprompt timeout: {e}")
        else:
            logging.warning(f"[{sid}] Row index missing in context during reprompt cleanup.")

    cleanup_call_data(sid)
    return str(resp)

# --- Configure Routes Function ---
def configure_routes(app):
    # Unchanged
    FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
    if not FLASK_SECRET_KEY:
        logging.warning("⚠️ FLASK_SECRET_KEY not set. Using default insecure key for session.")
        FLASK_SECRET_KEY = "default-insecure-"+os.urandom(12).hex()

    app.register_blueprint(routes_bp)
    app.secret_key = FLASK_SECRET_KEY
    logging.info("Routes blueprint registered and Flask secret key configured.")

# --- End routes.py ---
import os
import json
import asyncio
import websockets
import time
import base64
from fastapi import FastAPI, WebSocket, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv
from supabase import create_client, Client as SupabaseClient

load_dotenv()

app = FastAPI()

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Configuration
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
# TWILIO_PHONE_NUMBER removed - now dynamic
PORT = int(os.getenv("PORT", 5000))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123") # Default password

if not DEEPGRAM_API_KEY or not TWILIO_ACCOUNT_SID:
    print("Error: API keys must be set in .env")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("Warning: Supabase keys not set. Logs will fail.")

# Clients
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# Pydantic Models
class LoginRequest(BaseModel):
    password: str

class CallRequest(BaseModel):
    phone_number: str
    from_number: str # New: specific number to call from
    name: str
    carrier: str
    days_due: int
    due_date: str
    referral_info: str

# --- API Endpoints ---

@app.get("/")
async def root():
    return RedirectResponse(url="/static/login.html")

@app.post("/api/login")
async def login(req: LoginRequest):
    if req.password == ADMIN_PASSWORD:
        return {"status": "ok"}
    raise HTTPException(status_code=401, detail="Invalid password")

@app.post("/api/call")
async def trigger_call(req: CallRequest):
    try:
        print(f"Initiating call to {req.phone_number} from {req.from_number}...")
        
        # Verify from_number belongs to an org
        if supabase:
            res = supabase.table("phone_numbers").select("org_id").eq("phone_number", req.from_number).execute()
            if not res.data:
                raise HTTPException(status_code=400, detail="Invalid from_number: not registered to any organization.")
            org_id = res.data[0]['org_id']
        else:
            org_id = None # Fallback for local testing without DB?

        call = twilio_client.calls.create(
            to=req.phone_number,
            from_=req.from_number,
            url=f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME', 'localhost')}/incoming-call", # Auto-detect Render URL
        )
        
        # Create log entry in Supabase
        if supabase and org_id:
            data = {
                "call_sid": call.sid,
                "org_id": org_id,
                "phone_number": req.phone_number,
                "status": "initiated",
                "transcript": "",
                "customer_data": req.model_dump()
            }
            supabase.table("calls").insert(data).execute()
        
        return {"status": "initiated", "call_sid": call.sid}
    except Exception as e:
        print(f"Call failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/logs")
async def get_logs():
    if not supabase:
        return []
    # TODO: Filter logs by logged-in user's org (future)
    response = supabase.table("calls").select("*").order("created_at", desc=True).execute()
    return response.data

# --- Webhook & WebSocket ---

@app.post("/incoming-call")
async def incoming_call(request: Request):
    """
    Handle incoming calls from Twilio.
    Returns TwiML to connect the call to the Media Stream.
    """
    form_data = await request.form()
    to_number = form_data.get("To")
    
    print(f"Incoming call to: {to_number}")
    
    # Lookup Organization ID from Phone Number
    org_id = ""
    if supabase:
        res = supabase.table("phone_numbers").select("org_id").eq("phone_number", to_number).execute()
        if res.data:
            org_id = res.data[0]['org_id']
            print(f"Found Organization ID: {org_id}")
        else:
            print(f"Warning: No organization found for number {to_number}")
    
    response = VoiceResponse()
    
    # Start the Media Stream with org_id param
    connect = Connect()
    stream_url = f"wss://{request.headers.get('host')}/streams"
    if org_id:
        stream_url += f"?org_id={org_id}"
        
    stream = connect.stream(url=stream_url)
    response.append(connect)
    
    return Response(content=str(response), media_type="application/xml")


@app.websocket("/streams")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for Twilio Media Streams.
    Proxies audio between Twilio and Deepgram Voice Agent.
    """
    await websocket.accept()
    
    # Extract org_id from query params
    org_id = websocket.query_params.get("org_id")
    print(f"WebSocket connected. Org ID: {org_id}")

    # 1. Wait for Twilio Start Event to get context
    try:
        start_data = None
        while True:
            start_msg = await websocket.receive_text()
            msg_json = json.loads(start_msg)
            if msg_json.get('event') == 'start':
                start_data = msg_json
                break
            elif msg_json.get('event') == 'connected':
                print("Twilio connected event received.")
                continue
            else:
                print(f"Ignored event before start: {msg_json.get('event')}")

        call_sid = start_data['start']['callSid']
        stream_sid = start_data['start']['streamSid']
        print(f"Twilio Stream started: {stream_sid} for Call: {call_sid}")

        # 2. Fetch Configuration & Call Data
        system_prompt = "You are a helpful assistant." # Default
        voice_model = "aura-asteria-en"
        greeting = "Hello! How can I help you?"
        
        customer_name = "Valued Customer"
        days_due = 0
        due_date = "soon"
        referral_info = ""
        carrier = ""

        if supabase:
            # A. Fetch Agent Config for this Org
            if org_id:
                config_res = supabase.table("agent_configs").select("*").eq("org_id", org_id).execute()
                if config_res.data:
                    config = config_res.data[0]
                    system_prompt_template = config.get("system_prompt_template", system_prompt)
                    voice_model = config.get("voice_model", voice_model)
                    greeting = config.get("greeting", greeting)
                    
                    # Store template for formatting later
                    system_prompt = system_prompt_template

            # B. Update status & Fetch Customer Data (if outbound/logged call)
            supabase.table("calls").update({"status": "active"}).eq("call_sid", call_sid).execute()
            
            call_res = supabase.table("calls").select("customer_data").eq("call_sid", call_sid).execute()
            if call_res.data:
                data = call_res.data[0].get('customer_data', {})
                customer_name = data.get('name', customer_name)
                carrier = data.get('carrier', "")
                days_due = data.get('days_due', 0)
                due_date = data.get('due_date', "")
                referral_info = data.get('referral_info', "")

        # 3. Format System Prompt
        # Simple string replacement for now. 
        # In a real app, use a safer templating engine like Jinja2
        try:
            formatted_prompt = system_prompt.format(
                customer_name=customer_name,
                carrier=carrier,
                days_due=days_due,
                due_date=due_date,
                referral_info=referral_info
            )
        except KeyError as e:
            print(f"Warning: Missing key for prompt formatting: {e}")
            formatted_prompt = system_prompt # Fallback to unformatted
            
        print(f"Using System Prompt: {formatted_prompt}")

        # Deepgram Voice Agent URL
        deepgram_url = "wss://agent.deepgram.com/v1/agent/converse"
        
        # Queues for decoupling
        audio_queue = asyncio.Queue()
        streamsid_queue = asyncio.Queue()

        # Pre-populate queues since we consumed the start event
        streamsid_queue.put_nowait(stream_sid)

        # Connect to Deepgram
        async with websockets.connect(
            deepgram_url, 
            subprotocols=["token", DEEPGRAM_API_KEY]
        ) as deepgram_ws:
            print("Deepgram connected successfully.")

            # Configure Deepgram Agent
            config_message = {
                "type": "Settings",
                "audio": {
                    "input": {
                        "encoding": "mulaw",
                        "sample_rate": 8000,
                    },
                    "output": {
                        "encoding": "mulaw",
                        "sample_rate": 8000,
                        "container": "none",
                    },
                },
                "agent": {
                    "language": "en",
                    "listen": {
                        "provider": {
                            "type": "deepgram",
                            "model": "nova-3",
                            "keyterms": ["hello", "goodbye"]
                        }
                    },
                    "think": {
                        "provider": {
                            "type": "open_ai",
                            "model": "gpt-4o",
                        },
                        "prompt": formatted_prompt
                    },
                    "speak": {
                        "provider": {
                            "type": "deepgram",
                            "model": voice_model
                        }
                    },
                    "greeting": greeting
                }
            }
            
            print("Sending config to Deepgram...")
            await deepgram_ws.send(json.dumps(config_message))
            print("Config sent.")

            # --- Tasks ---

            async def deepgram_sender():
                print("deepgram_sender started")
                while True:
                    chunk = await audio_queue.get()
                    await deepgram_ws.send(chunk)

            async def deepgram_receiver():
                print("deepgram_receiver started")
                # Wait for stream SID
                streamsid = await streamsid_queue.get()
                
                current_transcript = ""

                async for message in deepgram_ws:
                    if isinstance(message, str):
                        # print(f"Deepgram Text: {message}")
                        decoded = json.loads(message)
                        msg_type = decoded.get('type')
                        
                        # Handle barge-in
                        if msg_type == 'UserStartedSpeaking':
                            print("User speaking, clearing audio...")
                            clear_message = {
                                "event": "clear",
                                "streamSid": streamsid
                            }
                            await websocket.send_text(json.dumps(clear_message))
                            
                            current_transcript += "\nUser: [Speaking...]"
                            if supabase:
                                supabase.table("calls").update({"transcript": current_transcript}).eq("call_sid", call_sid).execute()
                        
                        elif msg_type == 'ConversationText':
                            text = decoded.get('content')
                            role = decoded.get('role')
                            if text:
                                line = f"\n{role.capitalize()}: {text}"
                                current_transcript += line
                                if supabase:
                                    supabase.table("calls").update({"transcript": current_transcript}).eq("call_sid", call_sid).execute()
                                
                        elif msg_type == 'Error':
                             print(f"DEEPGRAM ERROR: {decoded}")

                        continue

                    # Handle Audio
                    raw_mulaw = message
                    media_message = {
                        "event": "media",
                        "streamSid": streamsid,
                        "media": {"payload": base64.b64encode(raw_mulaw).decode("ascii")},
                    }
                    await websocket.send_text(json.dumps(media_message))

            async def twilio_receiver():
                print("twilio_receiver started")
                # Buffer 20 * 160 bytes = 0.4s of audio
                BUFFER_SIZE = 20 * 160
                inbuffer = bytearray(b"")

                try:
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        # Note: 'start' event is already consumed in main loop
                        if data["event"] == "media":
                            media = data["media"]
                            chunk = base64.b64decode(media["payload"])
                            if media["track"] == "inbound":
                                inbuffer.extend(chunk)
                        elif data["event"] == "stop":
                            print("Twilio stream stopped.")
                            if supabase:
                                supabase.table("calls").update({"status": "completed"}).eq("call_sid", call_sid).execute()
                            break

                        # Check buffer
                        while len(inbuffer) >= BUFFER_SIZE:
                            chunk = inbuffer[:BUFFER_SIZE]
                            audio_queue.put_nowait(chunk)
                            inbuffer = inbuffer[BUFFER_SIZE:]
                except Exception as e:
                    print(f"Error in twilio_receiver: {e}")

            # Run tasks
            await asyncio.gather(
                deepgram_sender(),
                deepgram_receiver(),
                twilio_receiver()
            )

    except Exception as e:
        print(f"WebSocket Error: {e}")
    finally:
        print("WebSocket connection closed.")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

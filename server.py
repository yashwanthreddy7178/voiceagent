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
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
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
    name: str
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
        print(f"Initiating call to {req.phone_number}...")
        call = twilio_client.calls.create(
            to=req.phone_number,
            from_=TWILIO_PHONE_NUMBER,
            url=f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME', 'localhost')}/incoming-call", # Auto-detect Render URL
        )
        
        # Create log entry in Supabase
        if supabase:
            data = {
                "call_sid": call.sid,
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
    response = supabase.table("calls").select("*").order("created_at", desc=True).execute()
    return response.data

# --- Webhook & WebSocket ---

@app.post("/incoming-call")
async def incoming_call(request: Request):
    """
    Handle incoming calls from Twilio.
    Returns TwiML to connect the call to the Media Stream.
    """
    response = VoiceResponse()
    
    # Greet the user before connecting (optional, but helps with latency perception)
    # response.say("Connecting you to the agent...") 
    
    # Start the Media Stream
    connect = Connect()
    stream = connect.stream(url=f"wss://{request.headers.get('host')}/streams")
    response.append(connect)
    
    return Response(content=str(response), media_type="application/xml")


@app.websocket("/streams")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for Twilio Media Streams.
    Proxies audio between Twilio and Deepgram Voice Agent.
    """
    await websocket.accept()
    print("Twilio connected.")

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

        # 2. Lookup Call Data from Supabase
        customer_name = ""
        days_due = 0
        due_date = ""
        referral_info = ""
        
        if supabase:
            # Update status to active
            supabase.table("calls").update({"status": "active"}).eq("call_sid", call_sid).execute()
            
            # Fetch customer data
            response = supabase.table("calls").select("customer_data").eq("call_sid", call_sid).execute()
            if response.data:
                data = response.data[0].get('customer_data', {})
                customer_name = data.get('name', "")
                days_due = data.get('days_due', 0)
                due_date = data.get('due_date', "")
                referral_info = data.get('referral_info', "")
            else:
                print(f"Warning: No log found for call {call_sid}")
        
        # 3. Construct Dynamic Prompt
        system_prompt = f"You are Mike, calling from Total wireless new Kensington store. Your goal is to remind {customer_name} that their bill is due. Start by asking how they are doing. Wait for their response. Then, say 'I am just giving you a quick courtesy call to remind you that your bill will be due in {days_due} days that's on {due_date}. I just wanted to make sure everything's on track so there aren't any interruptions to your service.' Finally, mention: {referral_info}. Keep the conversation short, friendly, and professional."
        print(f"Using prompt: {system_prompt}")

        # Deepgram Voice Agent URL
        deepgram_url = "wss://agent.deepgram.com/v1/agent/converse"
        
        # Queues for decoupling
        audio_queue = asyncio.Queue()
        streamsid_queue = asyncio.Queue()
        # call_log_queue no longer needed as we write directly to DB

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
                        "prompt": system_prompt
                    },
                    "speak": {
                        "provider": {
                            "type": "deepgram",
                            "model": "aura-asteria-en"
                        }
                    },
                    "greeting": "Hello! This is Mike calling from Total wireless new Kensington store. How are you doing today?"
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

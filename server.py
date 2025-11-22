import os
import json
import asyncio
import websockets
import time
from fastapi import FastAPI, WebSocket, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Configuration
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
PORT = int(os.getenv("PORT", 5000))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123") # Default password

if not DEEPGRAM_API_KEY or not TWILIO_ACCOUNT_SID:
    print("Error: API keys must be set in .env")

# Twilio Client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# In-memory storage for call logs
# Structure: { "call_sid": { "timestamp": 123, "phone_number": "+123", "status": "active", "transcript": "" } }
calls_db = []

# Pydantic Models
class LoginRequest(BaseModel):
    password: str

class CallRequest(BaseModel):
    phone_number: str
    name: str = "Customer"
    days_due: int = 3

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
        
        # Create log entry
        log_entry = {
            "call_sid": call.sid,
            "timestamp": time.time(),
            "phone_number": req.phone_number,
            "status": "initiated",
            "transcript": "",
            "customer_data": req.model_dump()
        }
        calls_db.append(log_entry)
        
        return {"status": "initiated", "call_sid": call.sid}
    except Exception as e:
        print(f"Call failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/logs")
async def get_logs():
    return calls_db

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

    # Deepgram Voice Agent URL
    deepgram_url = "wss://agent.deepgram.com/v1/agent/converse"
    
    # Headers for Deepgram authentication
    headers = {
        "Authorization": f"Token {DEEPGRAM_API_KEY}"
    }

    try:
        print(f"Connecting to Deepgram: {deepgram_url}")
        # In websockets v13+, extra_headers was renamed to additional_headers
        async with websockets.connect(deepgram_url, additional_headers=headers) as deepgram_ws:
            print("Deepgram connected successfully.")

            # 1. Configure Deepgram Agent
            config_message = {
                "type": "SettingsConfiguration",
                "audio": {
                    "input": {
                        "encoding": "mulaw",
                        "sample_rate": 8000
                    },
                    "output": {
                        "encoding": "mulaw",
                        "sample_rate": 8000,
                        "container": "none"
                    }
                },
                "agent": {
                    "listen": { "model": "nova-2" },
                    "think": {
                        "provider": {
                            "type": "open_ai"
                        },
                        "model": "gpt-4o",
                        "instructions": "You are Yash, calling from a retail store. Your goal is to remind the customer that their bill is due. Start by asking how they are doing. Wait for their response. Then, gently remind them about the bill. Finally, mention that there is a referral program running where they can earn points. Keep the conversation short, friendly, and professional."
                    },
                    "speak": { "model": "aura-asteria-en" },
                    "greeting": "Hello! This is Yash from the store. How are you doing today?"
                }
            }
            print("Sending config to Deepgram...")
            await deepgram_ws.send(json.dumps(config_message))
            print("Config sent.")

            # Shared state
            stream_sid = None
            call_log = None

            async def receive_from_twilio():
                nonlocal stream_sid, call_log
                try:
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        if data['event'] == 'start':
                            stream_sid = data['start']['streamSid']
                            call_sid = data['start']['callSid']
                            print(f"Twilio Stream started: {stream_sid} for Call: {call_sid}")
                            
                            # Find and link log entry
                            for log in calls_db:
                                if log['call_sid'] == call_sid:
                                    call_log = log
                                    call_log['status'] = "active"
                                    break
                            
                        elif data['event'] == 'media':
                            import base64
                            audio_bytes = base64.b64decode(data['media']['payload'])
                            await deepgram_ws.send(audio_bytes)
                        elif data['event'] == 'stop':
                            print("Twilio stream stopped.")
                            if call_log: call_log['status'] = "completed"
                            break
                except Exception as e:
                    print(f"Error receiving from Twilio: {e}")

            async def receive_from_deepgram():
                nonlocal stream_sid, call_log
                try:
                    async for message in deepgram_ws:
                        if isinstance(message, bytes):
                            # Audio data
                            # print(f"Received {len(message)} bytes from Deepgram") # Debug audio flow
                            if stream_sid:
                                import base64
                                encoded_audio = base64.b64encode(message).decode("utf-8")
                                twilio_msg = {
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": { "payload": encoded_audio }
                                }
                                await websocket.send_text(json.dumps(twilio_msg))
                        else:
                            # Text message (logs, metadata, transcript)
                            msg_data = json.loads(message)
                            msg_type = msg_data.get('type')
                            print(f"Deepgram Message: {msg_type}")
                            
                            if msg_type == 'Error':
                                print(f"DEEPGRAM ERROR DETAILS: {json.dumps(msg_data, indent=2)}")
                            
                            if msg_type == 'UserStartedSpeaking':
                                if call_log: call_log['transcript'] += "\nUser: [Speaking...]"
                            elif msg_type == 'Results':
                                transcript = msg_data['channel']['alternatives'][0]['transcript']
                                if transcript and call_log:
                                    call_log['transcript'] += f"\nUser: {transcript}"
                            elif msg_type == 'AgentAudioDone':
                                pass
                                
                except Exception as e:
                    print(f"Error receiving from Deepgram: {e}")

            # Run both tasks
            await asyncio.gather(receive_from_twilio(), receive_from_deepgram())
    finally:
        print("WebSocket connection closed.")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

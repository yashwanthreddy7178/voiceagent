import os
import json
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request, Response
from fastapi.responses import HTMLResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# Configuration
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", 5000))

if not DEEPGRAM_API_KEY or not OPENAI_API_KEY:
    print("Error: DEEPGRAM_API_KEY and OPENAI_API_KEY must be set in .env")

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
                    "listen": {
                        "model": "nova-2"
                    },
                    "think": {
                        "provider": {
                            "type": "open_ai"
                        },
                        "model": "gpt-4o",
                        "instructions": "You are Yash, calling from a retail store. Your goal is to remind the customer that their bill is due in 3 days. Start by asking how they are doing in a friendly way. Wait for their response. Then, gently remind them about the bill. Finally, mention that there is a referral program running where they can earn points. Keep the conversation short, friendly, and professional."
                    },
                    "speak": {
                        "model": "aura-asteria-en" 
                    }
                }
            }
            print("Sending config to Deepgram...")
            await deepgram_ws.send(json.dumps(config_message))
            print("Config sent.")

            # Shared state
            stream_sid = None

            async def receive_from_twilio():
                nonlocal stream_sid
                try:
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        if data['event'] == 'start':
                            stream_sid = data['start']['streamSid']
                            print(f"Twilio Stream started: {stream_sid}")
                        elif data['event'] == 'media':
                            import base64
                            audio_bytes = base64.b64decode(data['media']['payload'])
                            await deepgram_ws.send(audio_bytes)
                        elif data['event'] == 'stop':
                            print("Twilio stream stopped.")
                            break
                except Exception as e:
                    print(f"Error receiving from Twilio: {e}")

            async def receive_from_deepgram():
                nonlocal stream_sid
                try:
                    async for message in deepgram_ws:
                        if isinstance(message, bytes):
                            # Audio data
                            if stream_sid:
                                import base64
                                encoded_audio = base64.b64encode(message).decode("utf-8")
                                twilio_msg = {
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {
                                        "payload": encoded_audio
                                    }
                                }
                                await websocket.send_text(json.dumps(twilio_msg))
                        else:
                            # Text message (logs, metadata)
                            print(f"Deepgram Text Message: {message}")
                except Exception as e:
                    print(f"Error receiving from Deepgram: {e}")

            # Run both tasks
            await asyncio.gather(receive_from_twilio(), receive_from_deepgram())
            
    except Exception as e:
        print(f"CRITICAL ERROR in WebSocket handler: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("WebSocket connection closed.")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

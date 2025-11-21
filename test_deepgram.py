import asyncio
import websockets
import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("DEEPGRAM_API_KEY")
URLS = [
    "wss://agent.deepgram.com/agent",
    "wss://agent.deepgram.com/v1/agent/converse",
    "wss://api.deepgram.com/agent",
    "wss://api.deepgram.com/v1/agent/converse"
]

async def test_connection():
    headers = {"Authorization": f"Token {API_KEY}"}
    for url in URLS:
        print(f"Testing {url}...")
        try:
            async with websockets.connect(url, additional_headers=headers) as ws:
                print(f"SUCCESS! Connected to {url}")
                await ws.close()
                return
        except Exception as e:
            print(f"Failed {url}: {e}")

if __name__ == "__main__":
    asyncio.run(test_connection())
